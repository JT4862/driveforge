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
        await ws.close(code=1008, reason="missing bearer token")
        return

    # Authenticate against the agents table. The check is fast +
    # synchronous (SHA-256 + constant-time compare); doing it before
    # accept() means an unauthenticated client never sees a 101
    # Switching Protocols response.
    with state.session_factory() as session:
        agent = fleet_mod.authenticate_agent(session, token)
        if agent is None:
            await ws.close(code=1008, reason="invalid or revoked token")
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
        # Keep the last snapshot around for display (marked offline
        # via `is_online()` timeout). It's more useful than vanishing
        # rows — operator can still see which drives WERE in the
        # agent when it went away.
        pass


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
        await ws.send_json(proto.HelloAckMsg(
            operator_version=DRIVEFORGE_VERSION,
            refused_reason=(
                f"protocol {hello.protocol_version} incompatible with "
                f"operator's {proto.PROTOCOL_VERSION}"
            ),
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
        await ws.close(code=1008, reason="agent_id mismatch")
        return

    # Acknowledge + record display_name / version / last_seen.
    from driveforge.version import __version__ as DRIVEFORGE_VERSION
    now = time.monotonic()
    state.remote_agents[agent_id] = RemoteAgentState(
        agent_id=agent_id,
        display_name=hello.display_name or stored_display,
        hostname=hello.hostname,
        agent_version=hello.agent_version,
        protocol_version=hello.protocol_version,
        connected_at=now,
        last_message_at=now,
        drives={},
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

    await ws.send_json(proto.HelloAckMsg(
        operator_version=DRIVEFORGE_VERSION,
    ).model_dump(mode="json"))

    # Steady-state message loop.
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
        else:
            # Forward-compat: unknown message types are logged + dropped.
            # Don't disconnect — a newer agent may speak a protocol minor
            # revision that adds messages this operator doesn't know.
            logger.debug("fleet: dropping unknown msg type=%r from %s", msg_type, agent_id)


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
