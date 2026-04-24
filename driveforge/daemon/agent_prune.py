"""Agent-side DB pruning (v0.10.7+).

Agents keep a local DB so the orchestrator can commit state at every
phase transition without a network round-trip (resilience against
operator outages). But JT's design intent from the original fleet
spec was clear:

    "I don't think the agent needs to keep its local history
     indefinitely. I think we can prune after the operator has it
     in their database."

v0.10.3 implemented the forward-and-ack half. v0.10.7 adds the
prune half.

### Retention policy

Keep on agent:
  - Any TestRun where `pending_fleet_forward=True` — operator
    hasn't ack'd it yet, replay-on-reconnect needs the row.
  - Any TestRun where `completed_at >= now - 24h` — lets the
    "just completed" dashboard flash + re-insert-same-session
    auto-enroll skip behave correctly.
  - The most-recent completed TestRun per drive serial, regardless
    of age — required for v0.8.0's regrade path which reads the
    source run's preserved throughput + SMART baseline. Without
    this, an agent that never forwards (standalone fell back to
    local after operator revoke) would lose regrade capability.
  - All Drive rows where the serial is currently present in
    lsblk, OR has a kept TestRun.

Prune from agent:
  - TestRun rows that are forwarded + older than 24h + not the
    most-recent per drive. SmartSnapshot and TelemetrySample
    rows cascade via the FK relationship.
  - Drive rows with zero remaining TestRuns AND not currently
    present.

### Cadence

Periodic task fires every 60 minutes. Cheap — a few queries over
indexed columns. Runs only when `fleet.role == "agent"`.

### Operator side

Operator does NOT prune. It's the canonical system of record for
fleet history; buyers scanning a cert QR months later must still
get a valid report page. Standalone installs also don't prune —
they ARE the canonical record for their drives.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from driveforge.db import models as m

logger = logging.getLogger(__name__)


# How long to keep a forwarded TestRun on the agent after
# completed_at. Short enough to matter; long enough to survive
# same-day re-insert-re-test flows.
DEFAULT_RETENTION_HOURS = 24

# How often the prune loop runs. Once per hour is plenty — pruning
# is not time-critical, and running more often just adds DB churn.
PRUNE_INTERVAL_S = 3600


@dataclass
class PruneStats:
    runs_deleted: int = 0
    drives_deleted: int = 0


def prune_once(state: Any, *, retention_hours: int = DEFAULT_RETENTION_HOURS) -> PruneStats:
    """One-shot prune. Exposed separately from the loop for testing
    and for `driveforge fleet prune` CLI use (future)."""
    stats = PruneStats()
    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)

    with state.session_factory() as session:
        # Build "keep-set" — the most-recent completed TestRun per drive
        # must always stay, regardless of age, for regrade support.
        # Using a subquery keyed on drive_serial + MAX(completed_at).
        keep_ids: set[int] = set()
        most_recent_per_drive = (
            session.query(
                m.TestRun.drive_serial,
                m.TestRun.id,
                m.TestRun.completed_at,
            )
            .filter(m.TestRun.completed_at.isnot(None))
            .all()
        )
        # Group by drive_serial, keep the max completed_at.id
        by_drive: dict[str, tuple[int, datetime]] = {}
        for serial, run_id, completed in most_recent_per_drive:
            if completed is None:
                continue
            prev = by_drive.get(serial)
            if prev is None or completed > prev[1]:
                by_drive[serial] = (run_id, completed)
        keep_ids = {v[0] for v in by_drive.values()}

        # Also keep anything in-flight or not-yet-forwarded.
        in_flight = (
            session.query(m.TestRun.id)
            .filter(
                (m.TestRun.completed_at.is_(None))
                | (m.TestRun.pending_fleet_forward.is_(True))
            )
            .all()
        )
        keep_ids.update(r[0] for r in in_flight)

        # Target set: forwarded, completed-more-than-retention-hours-ago,
        # NOT in keep_ids.
        prunable = (
            session.query(m.TestRun)
            .filter(m.TestRun.completed_at.isnot(None))
            .filter(m.TestRun.completed_at < cutoff)
            .filter(m.TestRun.pending_fleet_forward.is_(False))
            .all()
        )
        to_delete = [r for r in prunable if r.id not in keep_ids]
        if to_delete:
            for r in to_delete:
                session.delete(r)  # cascade: SmartSnapshot, TelemetrySample
            stats.runs_deleted = len(to_delete)
            session.commit()

        # Drive rows orphaned (no TestRuns after the delete) AND not
        # currently in active_phase OR device_basenames (present)
        # can be removed too. Standalone / operator paths never enter
        # this code (role check in the loop); agents safe to drop
        # drives that no longer have history + aren't present.
        present = set(state.active_phase.keys()) | set(state.device_basenames.keys())
        orphan_drives = (
            session.query(m.Drive)
            .outerjoin(m.TestRun, m.TestRun.drive_serial == m.Drive.serial)
            .filter(m.TestRun.id.is_(None))
            .all()
        )
        to_delete_drives = [d for d in orphan_drives if d.serial not in present]
        if to_delete_drives:
            for d in to_delete_drives:
                session.delete(d)
            stats.drives_deleted = len(to_delete_drives)
            session.commit()

    if stats.runs_deleted or stats.drives_deleted:
        logger.info(
            "agent-prune: deleted %d TestRun(s) + %d Drive row(s)",
            stats.runs_deleted, stats.drives_deleted,
        )
    return stats


async def prune_loop(state: Any) -> None:
    """Periodic agent-DB prune. Runs only when role=agent. Exits on
    daemon shutdown via CancelledError."""
    if state.settings.fleet.role != "agent":
        return
    while True:
        try:
            await asyncio.sleep(PRUNE_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        try:
            # Run the sync body in the drive executor so it doesn't
            # block the event loop during SQLite writes (the DB is
            # small but prune iterates every TestRun row on the box).
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                state.drive_command_executor, prune_once, state,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("agent-prune: iteration failed; will retry next tick")
