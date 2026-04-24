"""Agent-side fleet WebSocket client (v0.10.1+).

Runs as a long-lived asyncio task in the daemon lifespan when
`fleet.role == "agent"`. Connects to the operator's `/fleet/ws`
endpoint with a bearer token, sends a `hello` frame, then loops
sending drive snapshots every ~3s and heartbeats every ~30s.

Reconnect policy: exponential backoff, 1s → 60s cap. Local pipeline
execution is independent of the connection — a drive that's mid-
badblocks on an agent keeps running regardless of the operator
being reachable. Reconnect just re-attaches the dashboard view.

Intentional limitations (pushed to later v0.10.x):

- No replay of missed snapshots. Live state is fire-and-forget;
  the next snapshot (≤3s after reconnect) supersedes whatever
  the operator had cached.
- No command-receive path. Operator-issued commands (start pipeline,
  abort, identify) land in v0.10.2; for v0.10.1 the socket is
  upstream-only from a semantic standpoint even though bidirectional
  at the WS layer.
- No cert forwarding. Pipelines that complete on the agent during
  a disconnected window will need v0.10.3's WAL to replay — for
  v0.10.1, the cert stays local to the agent's DB.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any

from driveforge.core import fleet as fleet_mod
from driveforge.core import fleet_protocol as proto

logger = logging.getLogger(__name__)


# Cadence knobs. Deliberately module-level so tests can monkeypatch
# to tighter intervals without poking at the lifespan.
SNAPSHOT_INTERVAL_S = 3.0
HEARTBEAT_INTERVAL_S = 30.0
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
CONNECT_TIMEOUT_S = 15.0
# v0.10.3+: how often to scan for TestRuns flagged
# `pending_fleet_forward=True`. Cheap SQLite query against an
# indexed column — 10 s is plenty.
FORWARD_INTERVAL_S = 10.0


@dataclass
class ClientStatus:
    """Lightweight status read by the agent's own Settings page (v0.10.1+).

    Agents don't serve a web UI by convention, but the local `driveforge
    fleet status` CLI surfaces these fields for debugging. Also
    useful in tests.
    """
    connected: bool = False
    last_connected_at: float | None = None  # time.monotonic()
    last_error: str | None = None
    snapshots_sent: int = 0
    heartbeats_sent: int = 0
    reconnect_attempts: int = 0
    # v0.10.3+ — pipeline completions forwarded to operator.
    completions_sent: int = 0


def _http_url_to_ws_url(http_url: str) -> str:
    """Convert http://host:port → ws://host:port/fleet/ws.

    The operator URL the agent has configured points at the operator's
    HTTP base (what the enrollment endpoint lives at). The WebSocket
    endpoint is at the same host:port under /fleet/ws.
    """
    base = http_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif base.startswith("wss://") or base.startswith("ws://"):
        pass
    else:
        # Bare host:port with no scheme. Default to ws:// for LAN.
        base = "ws://" + base
    return f"{base}/fleet/ws"


class FleetClient:
    """One of these per agent daemon. Owns the reconnect loop."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.status = ClientStatus()
        self._stop = asyncio.Event()
        self._seq = 0

    async def run(self) -> None:
        """Outer reconnect loop. Returns only when `stop()` is called."""
        cfg = self.state.settings.fleet
        if cfg.role != "agent":
            return  # misconfigured; don't spin
        if not cfg.operator_url:
            logger.warning("fleet-client: agent role but no operator_url configured")
            return

        try:
            token = fleet_mod.read_agent_token(cfg.api_token_path)
        except fleet_mod.AgentTokenUnreadable as exc:
            # v0.10.6+: surface ownership / selinux issues loudly so
            # they don't turn into silent "agent not connecting"
            # mysteries. Pre-v0.10.6 this path crashed the lifespan
            # task with PermissionError that asyncio swallowed.
            logger.error("fleet-client: %s", exc)
            self.status.last_error = str(exc)
            return
        if not token:
            logger.warning(
                "fleet-client: agent role but no token at %s — run 'driveforge fleet join' to enroll",
                cfg.api_token_path,
            )
            return

        ws_url = _http_url_to_ws_url(cfg.operator_url)
        backoff = INITIAL_BACKOFF_S

        while not self._stop.is_set():
            try:
                await self._one_session(ws_url, token)
                # Normal close → reset backoff; reconnect quickly.
                backoff = INITIAL_BACKOFF_S
            except _FatalProtocolError as exc:
                # Operator refused the connection (protocol skew, token
                # rejected permanently, etc.). Don't reconnect; operator
                # has to act.
                logger.error("fleet-client: fatal — %s; not reconnecting", exc)
                self.status.last_error = str(exc)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                logger.info(
                    "fleet-client: disconnected (%s); reconnecting in %.1fs",
                    self.status.last_error, backoff,
                )
                self.status.reconnect_attempts += 1

            if self._stop.is_set():
                break
            # Sleep with cancellation support.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break  # stop() was called during the sleep
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def stop(self) -> None:
        self._stop.set()

    async def _one_session(self, ws_url: str, token: str) -> None:
        """Connect, run one session to completion (disconnect or error)."""
        import websockets  # transitively available via uvicorn[standard]

        logger.info("fleet-client: connecting to %s", ws_url)
        # `additional_headers` (websockets >= 14) / `extra_headers` (<14)
        # are the same thing; try both for cross-version compat.
        headers = {"Authorization": f"Bearer {token}"}
        connect_kwargs: dict[str, Any] = {
            "open_timeout": CONNECT_TIMEOUT_S,
            "close_timeout": 5.0,
            "ping_interval": None,  # we manage heartbeats at the protocol level
        }
        try:
            ws = await websockets.connect(  # type: ignore[attr-defined]
                ws_url, additional_headers=headers, **connect_kwargs,
            )
        except TypeError:
            ws = await websockets.connect(  # type: ignore[attr-defined]
                ws_url, extra_headers=headers, **connect_kwargs,
            )

        async with ws:
            await self._run_session(ws)

    async def _run_session(self, ws: Any) -> None:
        import json

        cfg = self.state.settings.fleet
        from driveforge.version import __version__ as DRIVEFORGE_VERSION
        agent_id = _extract_agent_id_from_token(cfg.api_token_path)
        if not agent_id:
            raise _FatalProtocolError("cannot parse agent_id from token")

        hello = proto.HelloMsg(
            agent_id=agent_id,
            display_name=cfg.display_name or socket.gethostname(),
            hostname=socket.gethostname(),
            agent_version=DRIVEFORGE_VERSION,
        )
        await ws.send(hello.model_dump_json())

        # Wait for hello_ack.
        ack_raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT_S)
        ack_data = json.loads(ack_raw)
        if ack_data.get("msg") != "hello_ack":
            raise _FatalProtocolError(f"expected hello_ack, got {ack_data.get('msg')!r}")
        if ack_data.get("refused_reason"):
            raise _FatalProtocolError(ack_data["refused_reason"])

        self.status.connected = True
        self.status.last_connected_at = time.monotonic()
        self.status.last_error = None
        # v0.10.9+ — capture operator's fleet-wide auto_enroll_mode
        # so hotplug decisions use the operator's setting instead
        # of this agent's stale local config. Forward-compat: older
        # operators don't send the field; leave None and agent
        # falls back to "off".
        op_mode = ack_data.get("auto_enroll_mode")
        if op_mode in ("off", "quick", "full"):
            self.state.fleet_operator_auto_enroll_mode = op_mode
            logger.info(
                "fleet-client: operator auto_enroll_mode = %s",
                op_mode,
            )
        logger.info(
            "fleet-client: connected (operator v%s)",
            ack_data.get("operator_version", "unknown"),
        )

        # Run sender + receiver + completion-forward concurrently.
        sender = asyncio.create_task(self._send_loop(ws))
        receiver = asyncio.create_task(self._receive_loop(ws))
        forward = asyncio.create_task(self._forward_completions_loop(ws))
        try:
            done, pending = await asyncio.wait(
                {sender, receiver, forward},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            # Surface the first error, if any.
            for t in done:
                if t.exception():
                    raise t.exception()  # type: ignore[misc]
        finally:
            self.status.connected = False

    async def _send_loop(self, ws: Any) -> None:
        """Emit drive snapshots every SNAPSHOT_INTERVAL_S and
        heartbeats every HEARTBEAT_INTERVAL_S."""
        last_hb = time.monotonic()
        while not self._stop.is_set():
            # Snapshot
            snap = self._build_snapshot()
            await ws.send(snap.model_dump_json())
            self.status.snapshots_sent += 1

            # Heartbeat if due
            now = time.monotonic()
            if now - last_hb >= HEARTBEAT_INTERVAL_S:
                await ws.send(proto.HeartbeatMsg().model_dump_json())
                self.status.heartbeats_sent += 1
                last_hb = now

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=SNAPSHOT_INTERVAL_S,
                )
                return  # stop() fired
            except asyncio.TimeoutError:
                continue

    async def _receive_loop(self, ws: Any) -> None:
        """Drain inbound messages and dispatch operator commands.

        v0.10.2+ — operator may send StartPipelineCmd / AbortCmd /
        IdentifyCmd / RegradeCmd frames at any time. Dispatch runs
        in a background task so one slow command doesn't block
        receipt of the next. Each command produces a CommandResultMsg
        reply sent upstream with `success` + optional `detail`.

        v0.10.3+ — operator also sends RunCompletedAckMsg to
        acknowledge receipt of a forwarded pipeline completion; the
        ack clears the agent's WAL flag on that TestRun."""
        import json
        async for raw_text in ws:
            try:
                raw = json.loads(raw_text) if isinstance(raw_text, (str, bytes)) else raw_text
            except (TypeError, ValueError):
                logger.warning("fleet-client: dropping non-JSON frame")
                continue
            msg_type = raw.get("msg") if isinstance(raw, dict) else None
            if msg_type in {"start_pipeline", "abort", "identify", "regrade", "update"}:
                # Fire-and-forget dispatch so a slow abort doesn't
                # block the next incoming command.
                asyncio.create_task(self._dispatch_command(ws, msg_type, raw))
            elif msg_type == "run_completed_ack":
                self._handle_completion_ack(raw)
            elif msg_type == "config_update":
                self._handle_config_update(raw)
            else:
                logger.debug("fleet-client: dropping unknown msg type=%r", msg_type)

    def _handle_config_update(self, raw: dict) -> None:
        """v0.10.9+ — apply an operator-pushed fleet config change.

        Currently scoped to `auto_enroll_mode`. When the operator's
        dashboard toggle changes, they broadcast this message to
        every connected agent; each agent updates its cached value,
        and the NEXT hotplug event respects the new mode.
        """
        try:
            msg = proto.ConfigUpdateMsg.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fleet-client: bad config_update: %s", exc)
            return
        if msg.auto_enroll_mode in ("off", "quick", "full"):
            prev = self.state.fleet_operator_auto_enroll_mode
            self.state.fleet_operator_auto_enroll_mode = msg.auto_enroll_mode
            logger.info(
                "fleet-client: auto_enroll_mode updated by operator: %s -> %s",
                prev, msg.auto_enroll_mode,
            )

    def _handle_completion_ack(self, raw: dict) -> None:
        """Clear pending_fleet_forward on the TestRun matching the
        acked completion_id. If the operator reported failure (retry
        later), leave the WAL flag set so the next forward-loop pass
        re-sends."""
        try:
            ack = proto.RunCompletedAckMsg.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fleet-client: bad run_completed_ack: %s", exc)
            return
        if not ack.success:
            logger.warning(
                "fleet-client: operator refused completion %s: %s — will retry",
                ack.completion_id, ack.detail,
            )
            return
        from driveforge.db import models as m
        with self.state.session_factory() as session:
            run = (
                session.query(m.TestRun)
                .filter_by(fleet_completion_id=ack.completion_id)
                .first()
            )
            if run is not None:
                run.pending_fleet_forward = False
                session.commit()
                logger.info(
                    "fleet-client: ack received for completion %s (run %d)",
                    ack.completion_id, run.id,
                )

    async def _forward_completions_loop(self, ws: Any) -> None:
        """Periodically scan for TestRuns flagged `pending_fleet_forward=True`
        and send them upstream. Runs at FORWARD_INTERVAL_S cadence;
        also runs once immediately at session start so any WAL
        entries queued during a disconnect window get flushed
        quickly.

        Exits only on socket failure (caller restarts the session).
        """
        # Initial flush — send anything queued while we were
        # disconnected.
        try:
            await self._send_pending_completions(ws)
        except Exception:  # noqa: BLE001
            logger.exception("fleet-client: initial completion flush errored")

        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=FORWARD_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._send_pending_completions(ws)
            except Exception:  # noqa: BLE001
                logger.exception("fleet-client: completion forward errored")

    async def _send_pending_completions(self, ws: Any) -> None:
        """Scan `test_runs WHERE pending_fleet_forward=True` and
        emit one RunCompletedMsg per row. Flag is cleared by
        `_handle_completion_ack` on operator ack, not here —
        send-without-ack is safe thanks to the operator-side
        idempotency on `fleet_completion_id`."""
        from driveforge.db import models as m
        with self.state.session_factory() as session:
            pending = (
                session.query(m.TestRun, m.Drive)
                .join(m.Drive, m.TestRun.drive_serial == m.Drive.serial)
                .filter(m.TestRun.pending_fleet_forward.is_(True))
                .order_by(m.TestRun.completed_at.asc())
                .limit(32)  # cap per tick to avoid flooding the socket
                .all()
            )
            batch = [(_build_run_completed_msg(run, drive)) for run, drive in pending]
        for msg in batch:
            await ws.send(msg.model_dump_json())
            self.status.completions_sent += 1
            logger.info(
                "fleet-client: forwarded completion %s (serial=%s, grade=%s)",
                msg.completion_id, msg.drive.serial, msg.run.grade,
            )

    async def _dispatch_command(self, ws: Any, msg_type: str, raw: dict) -> None:
        """Apply an operator command against this agent's local
        orchestrator + DB. Always emits a CommandResultMsg — success
        or failure — so the operator's audit log is complete."""
        try:
            cmd_id, success, detail = await self._apply_command(msg_type, raw)
        except Exception as exc:  # noqa: BLE001
            logger.exception("fleet-client: command dispatch error msg=%s", msg_type)
            cmd_id = (raw.get("cmd_id") if isinstance(raw, dict) else None) or "?"
            success = False
            detail = f"{type(exc).__name__}: {exc}"

        result = proto.CommandResultMsg(
            cmd_id=cmd_id, command=msg_type, success=success, detail=detail,
        )
        try:
            await ws.send(result.model_dump_json())
        except Exception:  # noqa: BLE001
            # Socket died mid-reply. The operator will notice via a
            # snapshot staleness / disconnect; no recovery action here.
            logger.debug("fleet-client: couldn't send command_result; socket closed")

    async def _apply_command(
        self, msg_type: str, raw: dict,
    ) -> tuple[str, bool, str | None]:
        """Validate + execute one command. Returns (cmd_id, success,
        detail). Caller wraps unexpected exceptions into a failure
        result."""
        from driveforge.core import drive as drive_mod
        from driveforge.db import models as m

        state = self.state

        # v0.11.4+ — UpdateCmd is dispatched independently of the
        # orchestrator. The local update path goes through
        # systemctl + the existing 50-driveforge-update.rules
        # polkit rule; no orchestrator state needed. Handled
        # FIRST so a daemon that's mid-startup (orchestrator not
        # attached yet) can still accept fleet-wide update pushes.
        if msg_type == "update":
            cmd = proto.UpdateCmd.model_validate(raw)
            from driveforge.core import updates as updates_mod
            ok, message = updates_mod.trigger_in_app_update()
            return cmd.cmd_id, ok, message

        orch = getattr(state, "orchestrator", None)
        if orch is None:
            cmd_id = raw.get("cmd_id", "?")
            return cmd_id, False, "local orchestrator not ready"

        if msg_type == "start_pipeline":
            cmd = proto.StartPipelineCmd.model_validate(raw)
            # Rebuild the drive_mod.Drive instance the orchestrator
            # expects. Prefer a freshly-discovered drive (has the
            # live device_path lsblk is currently using); fall back
            # to the DB row if the drive isn't on lsblk output yet
            # (transient enumeration race).
            found = None
            for d in drive_mod.discover():
                if d.serial == cmd.serial:
                    found = d
                    break
            if found is None:
                return cmd.cmd_id, False, f"drive {cmd.serial} not present on this agent"
            try:
                await orch.start_batch(
                    [found], source=cmd.source or "fleet-operator", quick=cmd.quick_mode,
                )
            except Exception as exc:  # noqa: BLE001
                return cmd.cmd_id, False, f"start_batch failed: {exc}"
            return cmd.cmd_id, True, None

        if msg_type == "abort":
            cmd = proto.AbortCmd.model_validate(raw)
            outcome = await orch.abort_drive(cmd.serial)
            status = outcome.get("status") if isinstance(outcome, dict) else None
            if status == "aborted":
                return cmd.cmd_id, True, outcome.get("note")
            # not_active / already_done / unknown → soft failure
            return cmd.cmd_id, False, f"abort {status}: {outcome.get('note')}"

        if msg_type == "identify":
            cmd = proto.IdentifyCmd.model_validate(raw)
            if cmd.on:
                # Need a drive_mod.Drive for identify_drive.
                discovered = {d.serial: d for d in drive_mod.discover()}
                drive_obj = discovered.get(cmd.serial)
                if drive_obj is None:
                    return cmd.cmd_id, False, f"drive {cmd.serial} not present on this agent"
                ok, message = await orch.identify_drive(drive_obj)
                return cmd.cmd_id, ok, message
            else:
                stopped = orch.stop_identify(cmd.serial)
                return cmd.cmd_id, stopped, (
                    "identify stopped" if stopped else "no identify blinker was running"
                )

        if msg_type == "regrade":
            cmd = proto.RegradeCmd.model_validate(raw)
            # Extract the core regrade logic without going through the
            # web handler (which does form parsing + flash redirects
            # that don't apply here). The orchestrator doesn't currently
            # own a regrade method (v0.8.0's logic lives in routes.py);
            # for v0.10.2 we call into a small helper that wraps the
            # same DB-level operation.
            from driveforge.core import fleet_regrade
            try:
                new_grade = await fleet_regrade.regrade_drive_locally(
                    state, cmd.serial,
                )
            except fleet_regrade.RegradeRefused as exc:
                return cmd.cmd_id, False, str(exc)
            except Exception as exc:  # noqa: BLE001
                return cmd.cmd_id, False, f"regrade errored: {exc}"
            return cmd.cmd_id, True, f"new grade: {new_grade}"

        cmd_id = raw.get("cmd_id", "?")
        return cmd_id, False, f"unknown command {msg_type!r}"

    def _build_snapshot(self) -> proto.DriveSnapshotMsg:
        """Capture the agent's live per-drive state from DaemonState.

        Mirrors the per-serial dicts the dashboard reads locally so
        the operator renders the remote drives with the same data
        density as its own.

        v0.10.6 fix: previously this iterated `DB rows ∪ active_phase`,
        which made EVERY drive ever enrolled on the agent appear on
        the operator's dashboard as "installed" — a drive-history
        dump, not a presence snapshot. Now iterates only over drives
        that (a) are currently discovered by lsblk OR (b) are in
        `active_phase` (covers the sub-second window during enroll
        where a drive is running but lsblk hasn't caught up). DB
        rows are consulted purely for metadata hydration — no row =
        fall through to minimal identity from live state.
        """
        from driveforge.core import drive as drive_mod
        from driveforge.db import models as m

        self._seq += 1
        drives: list[proto.DriveState] = []
        state = self.state

        # Currently-present drives via lsblk. This is what the local
        # dashboard's Installed section renders from.
        try:
            discovered = {d.serial: d for d in drive_mod.discover()}
        except Exception:  # noqa: BLE001
            # lsblk hiccup shouldn't kill the snapshot — report an
            # empty set and let the next tick recover.
            logger.exception("fleet-client: discover() failed; sending empty-present snapshot")
            discovered = {}

        active_serials = set(state.active_phase.keys())
        # Presence = discovered ∪ actively running. DO NOT include
        # DB rows here; that was the v0.10.0..v0.10.5 bug.
        present_serials = set(discovered.keys()) | active_serials

        if not present_serials:
            return proto.DriveSnapshotMsg(drives=[], seq=self._seq)

        with state.session_factory() as session:
            db_rows = {
                d.serial: d
                for d in session.query(m.Drive)
                .filter(m.Drive.serial.in_(present_serials))
                .all()
            }

        for serial in present_serials:
            live = discovered.get(serial)
            db_row = db_rows.get(serial)
            # Resolve each field preferring: live discovery → DB row →
            # None. Live discovery is freshest for transport + model
            # + capacity because it comes from lsblk right now; DB
            # row supplies manufacturer / firmware_version that
            # discover() doesn't compute.
            model = (live.model if live else None) or (db_row.model if db_row else "(unknown)")
            capacity = (live.capacity_bytes if live else 0) or (db_row.capacity_bytes if db_row else 0)
            transport = "unknown"
            if live is not None:
                transport = live.transport.value if hasattr(live.transport, "value") else str(live.transport)
            elif db_row is not None:
                transport = db_row.transport
            drives.append(proto.DriveState(
                serial=serial,
                model=model,
                capacity_bytes=capacity,
                transport=transport,
                manufacturer=(db_row.manufacturer if db_row else None)
                             or (live.manufacturer if live and getattr(live, "manufacturer", None) else None),
                rotational=(db_row.rotational if db_row else None),
                firmware_version=(db_row.firmware_version if db_row else None),
                device_basename=state.device_basenames.get(serial)
                                or (live.device_path.rsplit("/", 1)[-1] if live else None),
                phase=state.active_phase.get(serial),
                percent=state.active_percent.get(serial),
                sublabel=state.active_sublabel.get(serial),
                io_rate=state.active_io_rate.get(serial),
                drive_temp_c=state.active_drive_temp.get(serial),
                phase_change_ts_epoch=state.phase_change_ts.get(serial),
                identifying=_is_identifying_safe(state, serial),
            ))
        return proto.DriveSnapshotMsg(drives=drives, seq=self._seq)


def _is_identifying_safe(state: Any, serial: str) -> bool:
    """Read orch.is_identifying(serial) defensively. During startup
    the orchestrator attribute may not exist yet (lifespan hasn't
    run), and the snapshot builder is called from unit tests that
    bypass full boot."""
    orch = getattr(state, "orchestrator", None)
    if orch is None:
        return False
    try:
        return bool(orch.is_identifying(serial))
    except Exception:  # noqa: BLE001
        return False


def _extract_agent_id_from_token(path: Any) -> str | None:
    """The long-lived token is formatted `<agent_id>.<raw>`; pull the
    id half out so we don't have to round-trip through the DB to
    know our own identity during hello."""
    from pathlib import Path as _P
    p = _P(path) if not isinstance(path, _P) else path
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8").strip()
    if "." not in raw:
        return None
    return raw.split(".", 1)[0]


class _FatalProtocolError(Exception):
    """Raised when the agent should STOP reconnecting.

    Covers operator-refused cases like protocol skew or a permanently
    bad token. For transient errors (network flake, operator down)
    the reconnect loop retries with backoff — those don't raise this
    class.
    """


def _build_run_completed_msg(run: Any, drive: Any) -> proto.RunCompletedMsg:
    """Serialize a (TestRun, Drive) pair into a RunCompletedMsg for
    upstream forwarding. All TestRun columns the operator might want
    for rendering the cert label OR the buyer-transparency report
    go across the wire; this is roughly the projection the webhook
    payload uses, in pydantic form."""
    run_data = proto.CompletedRunData(
        run_id=run.id,
        drive_serial=run.drive_serial,
        batch_id=run.batch_id,
        bay=run.bay,
        phase=run.phase,
        started_at=run.started_at,
        completed_at=run.completed_at,
        grade=run.grade,
        triage_result=run.triage_result,
        power_on_hours_at_test=run.power_on_hours_at_test,
        reallocated_sectors=run.reallocated_sectors,
        current_pending_sector=run.current_pending_sector,
        offline_uncorrectable=run.offline_uncorrectable,
        pre_reallocated_sectors=run.pre_reallocated_sectors,
        pre_current_pending_sector=run.pre_current_pending_sector,
        smart_status_passed=run.smart_status_passed,
        rules=run.rules,
        report_url=run.report_url,
        label_printed=run.label_printed,
        quick_mode=run.quick_mode,
        throughput_mean_mbps=run.throughput_mean_mbps,
        throughput_p5_mbps=run.throughput_p5_mbps,
        throughput_p95_mbps=run.throughput_p95_mbps,
        throughput_pass_means=(
            list(run.throughput_pass_means) if run.throughput_pass_means else None
        ),
        error_message=run.error_message,
        log_tail=run.log_tail,
        interrupted_at_phase=run.interrupted_at_phase,
        sanitization_method=run.sanitization_method,
        lifetime_host_reads_bytes=run.lifetime_host_reads_bytes,
        lifetime_host_writes_bytes=run.lifetime_host_writes_bytes,
        wear_pct_used=run.wear_pct_used,
        available_spare_pct=run.available_spare_pct,
        end_to_end_error_count=run.end_to_end_error_count,
        command_timeout_count=run.command_timeout_count,
        reallocation_event_count=run.reallocation_event_count,
        nvme_critical_warning=run.nvme_critical_warning,
        nvme_media_errors=run.nvme_media_errors,
        self_test_has_past_failure=run.self_test_has_past_failure,
        drive_class=run.drive_class,
    )
    drive_data = proto.CompletedDriveData(
        serial=drive.serial,
        model=drive.model,
        manufacturer=drive.manufacturer,
        capacity_bytes=drive.capacity_bytes,
        transport=drive.transport,
        rotational=drive.rotational,
        firmware_version=drive.firmware_version,
    )
    return proto.RunCompletedMsg(
        completion_id=run.fleet_completion_id,
        drive=drive_data,
        run=run_data,
    )
