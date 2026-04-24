"""Operator-side fleet WebSocket endpoint (v0.10.1+).

Mounted on the operator's main FastAPI app at `/fleet/ws` by
`make_app` when `fleet.role == "operator"`. Agents dial in with a
long-lived bearer token in the `Authorization` header; the server
authenticates them against the `agents` table and then accepts a
stream of snapshot / heartbeat messages.

### Lifecycle

1. Agent opens WebSocket. Server reads the `Authorization: Bearer
   <composite_token>` header. If missing or invalid → close with
   policy-violation 1008.
2. Agent sends `hello` as the first frame. Server validates protocol
   version + updates the Agent row's last_seen_at + replies with
   `hello_ack` (including operator's version for the agent to log).
3. Agent sends a `drive_snapshot` (may be empty) followed by
   periodic snapshots + heartbeats.
4. Server stamps each frame into `state.remote_agents[agent_id]`,
   which the dashboard reads when rendering the drive grid.
5. On disconnect (either side): state.remote_agents entry stays
   until operator restart — stale state is fine, it's rendered with
   an "offline" badge once last_message_at ages past the heartbeat
   timeout.

Intentionally NOT in this module (yet):

- mTLS — v0.10.4 hardening scope.
- Outbound commands (start pipeline, abort, identify, regrade) —
  v0.10.2.
- Cert / run completion forwarding — v0.10.3.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from driveforge.core import fleet as fleet_mod
from driveforge.core import fleet_protocol as proto
from driveforge.daemon.state import RemoteAgentState, get_state
from driveforge.db import models as m

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fleet")


def _extract_bearer_token(ws: WebSocket) -> str | None:
    """Pull the bearer token from the WebSocket handshake headers.

    Accepts `Authorization: Bearer <token>` (standard). Some
    browser-based WS libraries can't set headers; as a convenience
    we also accept `?token=<composite>` in the query string. The
    token is the exact string written to `/etc/driveforge/agent.token`
    on the agent — i.e. `<agent_id>.<raw>`.
    """
    auth = ws.headers.get("authorization") or ws.headers.get("Authorization")
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    qp = ws.query_params.get("token")
    if qp:
        return qp.strip()
    return None


@router.websocket("/ws")
async def fleet_ws(ws: WebSocket) -> None:
    """Agent-facing WebSocket. One persistent connection per agent."""
    state = get_state()
    if state.settings.fleet.role != "operator":
        # Not serving fleet traffic on this node.
        await ws.close(code=1008, reason="fleet role is not operator")
        return

    token = _extract_bearer_token(ws)
    if not token:
        _record_refusal(state, "missing bearer token", token_agent_id=None, ws=ws)
        await ws.close(code=1008, reason="missing bearer token")
        return

    # Authenticate against the agents table. The check is fast +
    # synchronous (SHA-256 + constant-time compare); doing it before
    # accept() means an unauthenticated client never sees a 101
    # Switching Protocols response.
    # v0.10.4+: extract agent_id from the composite token (even when
    # auth fails) so the operator's refusal log can point at which
    # agent_id was presented.
    token_agent_id = token.split(".", 1)[0] if "." in token else None
    with state.session_factory() as session:
        agent = fleet_mod.authenticate_agent(session, token)
        if agent is None:
            # Distinguish revoked-but-present vs genuinely unknown.
            raw_row = session.get(m.Agent, token_agent_id) if token_agent_id else None
            reason = (
                "token revoked" if raw_row is not None and raw_row.revoked_at is not None
                else "invalid token"
            )
            _record_refusal(state, reason, token_agent_id=token_agent_id, ws=ws)
            await ws.close(code=1008, reason=reason)
            return
        agent_id = agent.id
        stored_display = agent.display_name

    await ws.accept()
    logger.info("fleet: agent connected agent_id=%s", agent_id)

    # Drop any prior live-state snapshot; the agent will send a
    # fresh one on hello. Stale state from a previous connection is
    # confusing.
    state.remote_agents.pop(agent_id, None)
    state.remote_snapshot_seq.pop(agent_id, None)

    try:
        await _handle_session(ws, state, agent_id, stored_display)
    except WebSocketDisconnect:
        logger.info("fleet: agent disconnected agent_id=%s", agent_id)
    except Exception:
        logger.exception("fleet: agent session errored agent_id=%s", agent_id)
    finally:
        # v0.10.4+ — clear the socket reference so `kick_agent_session`
        # doesn't try to double-close. Keep the rest of the
        # remote_agents entry for display purposes; dashboard marks
        # the agent offline via is_online() timeout.
        ra = state.remote_agents.get(agent_id)
        if ra is not None:
            ra.ws = None
            if ra.outbound_queue is not None:
                # Don't keep a queue around for a dead session; next
                # connect creates a fresh one.
                ra.outbound_queue = None


def _record_refusal(
    state: Any, reason: str, *, token_agent_id: str | None, ws: WebSocket,
) -> None:
    """Stash a connection refusal for the operator's Agents page.

    Bounded to the last 32 entries so a misbehaving agent that
    reconnects in a tight loop doesn't OOM the daemon."""
    from datetime import UTC, datetime
    remote_ip = None
    try:
        client = getattr(ws, "client", None)
        if client is not None:
            remote_ip = client.host
    except Exception:  # noqa: BLE001
        pass
    state.fleet_refusals.append({
        "ts": datetime.now(UTC).isoformat(),
        "reason": reason,
        "token_agent_id": token_agent_id,
        "remote_ip": remote_ip,
    })
    # Cap buffer.
    if len(state.fleet_refusals) > 32:
        del state.fleet_refusals[0:len(state.fleet_refusals) - 32]
    logger.info(
        "fleet: connection refused (reason=%s, agent_id=%s, ip=%s)",
        reason, token_agent_id, remote_ip,
    )


async def kick_agent_session(state: Any, agent_id: str, reason: str) -> bool:
    """v0.10.4+ — forcibly close an agent's active WebSocket session.

    Called from Settings → Agents when the operator revokes or
    rotates a credential. The agent's reconnect loop then sees the
    close, tries to reconnect with the now-invalid token, and gets
    refused at the handshake — the refusal lands in
    `state.fleet_refusals` so the operator sees confirmation on the
    Agents page.

    Returns True if a session was kicked, False if the agent wasn't
    connected.
    """
    ra = state.remote_agents.get(agent_id)
    if ra is None or ra.ws is None:
        return False
    ws = ra.ws
    try:
        await ws.close(code=1008, reason=reason)
    except Exception:  # noqa: BLE001
        logger.exception("fleet: kick close failed for %s", agent_id)
    ra.ws = None
    return True


async def _handle_session(
    ws: WebSocket, state: Any, agent_id: str, stored_display: str,
) -> None:
    """Main message loop for one authenticated agent."""
    # First frame must be hello.
    first = await ws.receive_json()
    try:
        hello = proto.HelloMsg.model_validate(first)
    except ValidationError as exc:
        logger.warning("fleet: agent %s sent invalid hello: %s", agent_id, exc)
        await ws.close(code=1003, reason="expected hello")
        return

    if not proto.is_protocol_compatible(hello.protocol_version):
        # Major-version skew → refuse. Agent logs the reason and stops
        # reconnecting until operator action.
        from driveforge.version import __version__ as DRIVEFORGE_VERSION
        reason = (
            f"protocol {hello.protocol_version} incompatible with "
            f"operator's {proto.PROTOCOL_VERSION}"
        )
        _record_refusal(state, reason, token_agent_id=agent_id, ws=ws)
        await ws.send_json(proto.HelloAckMsg(
            operator_version=DRIVEFORGE_VERSION,
            refused_reason=reason,
        ).model_dump(mode="json"))
        await ws.close(code=1008, reason="protocol skew")
        return

    # Agent identity reconciliation. The agent_id in the token is the
    # source of truth — if the agent's self-declared agent_id disagrees,
    # that's a bug or tampering.
    if hello.agent_id != agent_id:
        logger.warning(
            "fleet: agent_id mismatch token=%s hello=%s — closing",
            agent_id, hello.agent_id,
        )
        _record_refusal(state, "agent_id mismatch", token_agent_id=agent_id, ws=ws)
        await ws.close(code=1008, reason="agent_id mismatch")
        return

    # Acknowledge + record display_name / version / last_seen.
    # v0.10.2+: outbound queue is created inside the handler (inside
    # the event loop) so it binds to the right loop even in
    # multi-worker uvicorn deployments.
    from driveforge.version import __version__ as DRIVEFORGE_VERSION
    now = time.monotonic()
    outbound: asyncio.Queue = asyncio.Queue(maxsize=256)
    state.remote_agents[agent_id] = RemoteAgentState(
        agent_id=agent_id,
        display_name=hello.display_name or stored_display,
        hostname=hello.hostname,
        agent_version=hello.agent_version,
        protocol_version=hello.protocol_version,
        connected_at=now,
        last_message_at=now,
        drives={},
        outbound_queue=outbound,
        ws=ws,  # v0.10.4+ — referenced by `kick_agent_session`
    )
    with state.session_factory() as session:
        # Persist version + display-name drift for the Agents page.
        from driveforge.db import models as m
        row = session.get(m.Agent, agent_id)
        if row is not None:
            row.version = hello.agent_version
            if hello.display_name:
                row.display_name = hello.display_name
            if hello.hostname:
                row.hostname = hello.hostname
            from datetime import UTC, datetime
            row.last_seen_at = datetime.now(UTC)
            session.commit()

    # v0.10.9+ — hand the agent our current fleet-wide config so it
    # mirrors our auto_enroll_mode instead of consulting its stale
    # local copy. Agents read this from state.fleet_operator_auto_enroll_mode
    # when their hotplug handler fires.
    await ws.send_json(proto.HelloAckMsg(
        operator_version=DRIVEFORGE_VERSION,
        auto_enroll_mode=state.settings.daemon.auto_enroll_mode,
    ).model_dump(mode="json"))

    # v0.10.2+ run sender + receiver concurrently. Sender drains the
    # outbound command queue; receiver handles inbound frames.
    sender_task = asyncio.create_task(_sender_loop(ws, outbound))
    try:
        await _receiver_loop(ws, state, agent_id)
    finally:
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass


async def _sender_loop(ws: WebSocket, outbound: asyncio.Queue) -> None:
    """Drain outbound_queue and write each JSON-encoded command to
    the WebSocket. One task per active session."""
    while True:
        payload = await outbound.get()
        try:
            await ws.send_text(payload)
        except Exception:  # noqa: BLE001
            logger.exception("fleet: sender_loop error; aborting session")
            return


async def _receiver_loop(ws: WebSocket, state: Any, agent_id: str) -> None:
    """Steady-state inbound-message loop."""
    while True:
        raw = await ws.receive_json()
        msg_type = raw.get("msg") if isinstance(raw, dict) else None
        if msg_type == "drive_snapshot":
            try:
                snap = proto.DriveSnapshotMsg.model_validate(raw)
            except ValidationError as exc:
                logger.warning("fleet: bad snapshot from %s: %s", agent_id, exc)
                continue
            _apply_snapshot(state, agent_id, snap)
        elif msg_type == "heartbeat":
            # Just stamp last_message_at; nothing else to do.
            ra = state.remote_agents.get(agent_id)
            if ra is not None:
                ra.last_message_at = time.monotonic()
        elif msg_type == "command_result":
            try:
                result = proto.CommandResultMsg.model_validate(raw)
            except ValidationError as exc:
                logger.warning("fleet: bad command_result from %s: %s", agent_id, exc)
                continue
            _record_command_result(state, agent_id, result)
        elif msg_type == "run_completed":
            # v0.10.3+ — agent forwarded a pipeline completion.
            # Spawn a task so the ingest doesn't block the receiver
            # (auto-print can take 20-30s per Brother QL cycle).
            asyncio.create_task(_ingest_run_completed(ws, state, agent_id, raw))
        else:
            # Forward-compat: unknown message types are logged + dropped.
            # Don't disconnect — a newer agent may speak a protocol minor
            # revision that adds messages this operator doesn't know.
            logger.debug("fleet: dropping unknown msg type=%r from %s", msg_type, agent_id)


async def _ingest_run_completed(
    ws: WebSocket, state: Any, agent_id: str, raw: dict,
) -> None:
    """v0.10.3+ — receive a forwarded pipeline completion from an
    agent. Upsert Drive + TestRun rows into the operator's DB with
    `last_host_id` / `host_id` set, run auto-print, send ack.

    Idempotent via `fleet_completion_id` — if the row already exists
    (agent replayed after a dropped ack), the handler recognizes it
    and just re-sends the ack without duplicating the DB row or
    re-printing."""
    try:
        msg = proto.RunCompletedMsg.model_validate(raw)
    except ValidationError as exc:
        logger.warning("fleet: bad run_completed from %s: %s", agent_id, exc)
        return

    from driveforge.db import models as m

    try:
        already_have_it = False
        with state.session_factory() as session:
            existing = (
                session.query(m.TestRun)
                .filter_by(fleet_completion_id=msg.completion_id)
                .first()
            )
            if existing is not None:
                # Already persisted; just ack again.
                already_have_it = True
            else:
                # Upsert drive identity first (may or may not exist).
                drive_row = session.get(m.Drive, msg.drive.serial)
                if drive_row is None:
                    drive_row = m.Drive(
                        serial=msg.drive.serial,
                        model=msg.drive.model,
                        manufacturer=msg.drive.manufacturer,
                        capacity_bytes=msg.drive.capacity_bytes,
                        transport=msg.drive.transport,
                        rotational=msg.drive.rotational,
                        firmware_version=msg.drive.firmware_version,
                    )
                    session.add(drive_row)
                else:
                    # Keep drive metadata fresh from the remote view —
                    # model/manufacturer/firmware may refine as smartctl
                    # lands new data on the agent.
                    drive_row.model = msg.drive.model
                    drive_row.manufacturer = (
                        msg.drive.manufacturer or drive_row.manufacturer
                    )
                    drive_row.capacity_bytes = msg.drive.capacity_bytes
                    drive_row.transport = msg.drive.transport
                    if msg.drive.rotational is not None:
                        drive_row.rotational = msg.drive.rotational
                    drive_row.firmware_version = (
                        msg.drive.firmware_version or drive_row.firmware_version
                    )
                drive_row.last_host_id = agent_id
                from datetime import UTC, datetime
                drive_row.last_host_seen_at = datetime.now(UTC)

                # Insert TestRun. host_id = agent_id so history + cert
                # generation can correctly attribute this run to the
                # remote agent.
                r = msg.run
                new_run = m.TestRun(
                    drive_serial=r.drive_serial,
                    batch_id=None,  # batch IDs are agent-local; drop
                    bay=r.bay,
                    phase=r.phase,
                    started_at=r.started_at,
                    completed_at=r.completed_at,
                    grade=r.grade,
                    triage_result=r.triage_result,
                    power_on_hours_at_test=r.power_on_hours_at_test,
                    reallocated_sectors=r.reallocated_sectors,
                    current_pending_sector=r.current_pending_sector,
                    offline_uncorrectable=r.offline_uncorrectable,
                    pre_reallocated_sectors=r.pre_reallocated_sectors,
                    pre_current_pending_sector=r.pre_current_pending_sector,
                    smart_status_passed=r.smart_status_passed,
                    rules=r.rules,
                    report_url=r.report_url,
                    label_printed=r.label_printed,
                    quick_mode=r.quick_mode,
                    throughput_mean_mbps=r.throughput_mean_mbps,
                    throughput_p5_mbps=r.throughput_p5_mbps,
                    throughput_p95_mbps=r.throughput_p95_mbps,
                    throughput_pass_means=r.throughput_pass_means,
                    error_message=r.error_message,
                    log_tail=r.log_tail,
                    interrupted_at_phase=r.interrupted_at_phase,
                    sanitization_method=r.sanitization_method,
                    lifetime_host_reads_bytes=r.lifetime_host_reads_bytes,
                    lifetime_host_writes_bytes=r.lifetime_host_writes_bytes,
                    wear_pct_used=r.wear_pct_used,
                    available_spare_pct=r.available_spare_pct,
                    end_to_end_error_count=r.end_to_end_error_count,
                    command_timeout_count=r.command_timeout_count,
                    reallocation_event_count=r.reallocation_event_count,
                    nvme_critical_warning=r.nvme_critical_warning,
                    nvme_media_errors=r.nvme_media_errors,
                    self_test_has_past_failure=r.self_test_has_past_failure,
                    drive_class=r.drive_class,
                    host_id=agent_id,
                    fleet_completion_id=msg.completion_id,
                    # Don't re-flag pending_fleet_forward on the operator side.
                    pending_fleet_forward=False,
                )
                session.add(new_run)
                session.commit()
                session.refresh(new_run)
                session.refresh(drive_row)
                # Fire auto-print on the operator's own printer.
                # Detach row instances before passing to the executor
                # (SQLAlchemy objects aren't thread-safe).
                _fire_operator_autoprint(state, drive_row, new_run)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "fleet: run_completed ingest failed for completion_id=%s",
            msg.completion_id,
        )
        # Signal failure so the agent keeps the WAL entry for retry.
        await ws.send_text(proto.RunCompletedAckMsg(
            completion_id=msg.completion_id,
            success=False,
            detail=f"operator ingest errored: {exc}",
        ).model_dump_json())
        return

    await ws.send_text(proto.RunCompletedAckMsg(
        completion_id=msg.completion_id, success=True,
    ).model_dump_json())
    if already_have_it:
        logger.info(
            "fleet: re-ack for already-ingested completion_id=%s from agent %s",
            msg.completion_id, agent_id,
        )
    else:
        logger.info(
            "fleet: ingested completion_id=%s from agent %s (serial=%s, grade=%s)",
            msg.completion_id, agent_id, msg.drive.serial, msg.run.grade,
        )


def _fire_operator_autoprint(state: Any, drive: Any, run: Any) -> None:
    """Fire-and-forget auto-print on the operator's printer for a
    remote-originated run. Matches the agent-side auto-print gate:
    grade present, not quick-mode, printer model configured,
    auto_print toggle True.

    Print failures are logged but do NOT unwind the DB commit —
    the cert data is preserved on the operator's history page;
    operator can click Print Label manually if the printer comes
    back online."""
    if not run.grade or run.quick_mode:
        return
    pc = state.settings.printer
    if not pc.model or not getattr(pc, "auto_print", True):
        return

    loop = asyncio.get_event_loop()

    def _go():
        try:
            from driveforge.core import printer as printer_mod
            ok, msg = printer_mod.auto_print_cert_for_run(state, drive, run)
            if ok:
                logger.info(
                    "fleet: auto-printed operator cert for %s (%s)",
                    drive.serial, msg,
                )
            else:
                logger.warning(
                    "fleet: operator auto-print failed for %s: %s",
                    drive.serial, msg,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "fleet: operator auto-print crashed for %s", drive.serial,
            )

    loop.run_in_executor(state.drive_command_executor, _go)


def _record_command_result(state: Any, agent_id: str, result: proto.CommandResultMsg) -> None:
    """Stash a command reply on the agent's RemoteAgentState for
    operator-side surfacing. Capped buffer; oldest drops off.

    Dashboard flash area polls this on the next request and renders
    a warning banner for failures — e.g. "abort refused on SERIAL:
    drive in secure_erase phase"."""
    ra = state.remote_agents.get(agent_id)
    if ra is None:
        return
    buf = ra.recent_command_results
    buf.append(result)
    # Cap at 64 — arbitrary bound, plenty for a few dozen rapid
    # commands while keeping memory bounded.
    if len(buf) > 64:
        del buf[0:len(buf) - 64]
    if not result.success:
        logger.warning(
            "fleet: command_result FAILED agent=%s cmd=%s cmd_id=%s detail=%s",
            agent_id, result.command, result.cmd_id, result.detail,
        )
    else:
        logger.info(
            "fleet: command_result ok agent=%s cmd=%s cmd_id=%s",
            agent_id, result.command, result.cmd_id,
        )


def _apply_snapshot(state: Any, agent_id: str, snap: proto.DriveSnapshotMsg) -> None:
    """Replace the agent's drive-state dict with the snapshot.

    Out-of-order frames (snap.seq <= last seen) are dropped silently —
    the next frame arrives within 3 s under normal conditions.
    """
    prev_seq = state.remote_snapshot_seq.get(agent_id, 0)
    if snap.seq <= prev_seq:
        logger.debug(
            "fleet: dropping stale snapshot from %s seq=%d prev=%d",
            agent_id, snap.seq, prev_seq,
        )
        return
    state.remote_snapshot_seq[agent_id] = snap.seq
    ra = state.remote_agents.get(agent_id)
    if ra is None:
        # Session was closed concurrently; drop.
        return
    ra.drives = {d.serial: d for d in snap.drives}
    ra.last_message_at = time.monotonic()


# ---------------------------- dashboard helpers


def online_agents(state: Any, *, timeout_s: float = 120.0) -> list[RemoteAgentState]:
    """List agents whose most-recent frame is within the heartbeat
    window. Dashboard reads this to decide the host filter + online
    badge state."""
    now = time.monotonic()
    return [
        ra for ra in state.remote_agents.values()
        if ra.is_online(now, timeout_s=timeout_s)
    ]


def all_known_agents(state: Any) -> list[RemoteAgentState]:
    """All agents the operator has heard from since boot, including
    ones that have since gone offline. Offline agents still show
    their last-seen drives on the dashboard with a muted host
    badge."""
    return list(state.remote_agents.values())


# ---------------------------- v0.10.2+ command dispatch


def find_agent_for_serial(state: Any, serial: str) -> str | None:
    """Return the agent_id that most recently reported this serial,
    or None if no agent has it.

    The dashboard's POST handlers (abort, identify, regrade, start
    batch) call this to decide whether to dispatch locally or forward
    to an agent. O(number of agents × drives per agent), trivially
    cheap at homelab fleet scale — can cache later if needed."""
    for agent_id, ra in state.remote_agents.items():
        if serial in ra.drives:
            return agent_id
    return None


class CommandDispatchError(Exception):
    """Raised when a command can't be enqueued — agent unknown,
    offline too long, or outbound queue full (agent pathologically
    slow to drain)."""


async def send_command_to_agent(
    state: Any, agent_id: str, command: Any,
) -> None:
    """Enqueue a command for the target agent. Serializes to JSON
    and drops on the session's outbound queue. Fire-and-forget —
    the caller doesn't wait for the CommandResultMsg reply; that
    arrives asynchronously and lands in `recent_command_results`.

    Raises CommandDispatchError if the agent has no active session
    (no outbound queue — the session hasn't started OR was torn
    down). Callers should catch + surface a user-facing error
    ("agent X is offline — command not sent").
    """
    ra = state.remote_agents.get(agent_id)
    if ra is None:
        raise CommandDispatchError(f"agent {agent_id} not connected")
    if ra.outbound_queue is None:
        raise CommandDispatchError(f"agent {agent_id} has no outbound queue (mid-handshake?)")
    # Queue full = agent can't drain. This only happens if the agent's
    # event loop is wedged or the connection is saturated; both are
    # weird enough that raising is the right behavior.
    try:
        ra.outbound_queue.put_nowait(command.model_dump_json())
    except asyncio.QueueFull as exc:
        raise CommandDispatchError(
            f"agent {agent_id} outbound queue full (256 items)"
        ) from exc


def drain_command_failures(state: Any) -> list[Any]:
    """Pop any failed CommandResultMsg entries across all agents for
    display in the next dashboard render. Called by the dashboard
    route once per request; list is consumed (not peeked) so each
    failure flashes exactly once.

    Success results stay in the per-agent buffer for debugging /
    audit; only failures are surfaced to the operator.
    """
    failures: list[Any] = []
    for ra in state.remote_agents.values():
        keep: list[Any] = []
        for r in ra.recent_command_results:
            if not r.success:
                failures.append((ra, r))
            else:
                keep.append(r)
        ra.recent_command_results = keep
    return failures
