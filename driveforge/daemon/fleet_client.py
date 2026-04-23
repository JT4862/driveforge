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

        token = fleet_mod.read_agent_token(cfg.api_token_path)
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
        logger.info(
            "fleet-client: connected (operator v%s)",
            ack_data.get("operator_version", "unknown"),
        )

        # Run sender + receiver concurrently.
        sender = asyncio.create_task(self._send_loop(ws))
        receiver = asyncio.create_task(self._receive_loop(ws))
        try:
            done, pending = await asyncio.wait(
                {sender, receiver},
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
        """Drain inbound messages. v0.10.1 operator doesn't send
        anything after hello_ack — this loop mainly exists to detect
        the operator closing the connection."""
        async for _raw in ws:
            # v0.10.2+ will dispatch commands here. For v0.10.1 we
            # just keep the socket readable so WS pings/pongs work.
            continue

    def _build_snapshot(self) -> proto.DriveSnapshotMsg:
        """Capture the agent's live per-drive state from DaemonState.

        Mirrors the per-serial dicts the dashboard reads locally so
        the operator renders the remote drives with the same data
        density as its own.
        """
        self._seq += 1
        drives: list[proto.DriveState] = []
        state = self.state

        # Union of "drive is known to DB" ∪ "drive is currently active".
        # Active drives might not be in DB yet during a fresh enroll,
        # so snapshot both sets.
        from driveforge.db import models as m
        active_serials = set(state.active_phase.keys())
        with state.session_factory() as session:
            db_drives = {d.serial: d for d in session.query(m.Drive).all()}

        serials = set(db_drives.keys()) | active_serials

        for serial in serials:
            d = db_drives.get(serial)
            if d is None:
                # Active but not yet in DB — synthesize minimal identity
                # from whatever the state dicts carry. The operator's
                # renderer treats missing fields defensively.
                drives.append(proto.DriveState(
                    serial=serial,
                    model="(unknown)",
                    capacity_bytes=0,
                    transport="unknown",
                    device_basename=state.device_basenames.get(serial),
                    phase=state.active_phase.get(serial),
                    percent=state.active_percent.get(serial),
                    sublabel=state.active_sublabel.get(serial),
                    io_rate=state.active_io_rate.get(serial),
                    drive_temp_c=state.active_drive_temp.get(serial),
                    phase_change_ts_epoch=state.phase_change_ts.get(serial),
                ))
                continue
            drives.append(proto.DriveState(
                serial=serial,
                model=d.model,
                capacity_bytes=d.capacity_bytes,
                transport=d.transport,
                manufacturer=d.manufacturer,
                rotational=d.rotational,
                firmware_version=d.firmware_version,
                device_basename=state.device_basenames.get(serial),
                phase=state.active_phase.get(serial),
                percent=state.active_percent.get(serial),
                sublabel=state.active_sublabel.get(serial),
                io_rate=state.active_io_rate.get(serial),
                drive_temp_c=state.active_drive_temp.get(serial),
                phase_change_ts_epoch=state.phase_change_ts.get(serial),
            ))
        return proto.DriveSnapshotMsg(drives=drives, seq=self._seq)


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
