"""Frozen-SSD remediation state + operator-facing checklist.

v0.6.9+. Companion to v0.6.7's HDD badblocks-only fallback.

Problem class
-------------
libata issues SECURITY FREEZE LOCK during udev probe on every ATA
drive that supports it. A frozen drive refuses SECURITY ERASE UNIT
no matter which kernel path issues it (SAT passthrough via sg_raw
OR native ATA via hdparm) — both return ABRT. v0.6.3 captured the
signature in `core.erase.is_libata_freeze_pattern`.

For **HDDs** this is handled by v0.6.7: the pipeline continues to
badblocks, which writes a 4-pattern destructive overwrite. That IS
NIST 800-88 Clear for magnetic media — a legitimate sanitization.
The cert label honestly says "Wipe: NIST 800-88 Clear (4-pass)"
instead of the default "NIST 800-88 + 4-pass" wording that implies
secure_erase ran.

For **SSDs** badblocks-only is NOT safe. Wear leveling maps logical
sectors to physical NAND pages dynamically; a 4-pattern overwrite
of logical sectors leaves stale data on over-provisioned NAND
pages that logical writes never touch. NIST 800-88 specifically
excludes overwrite for flash media. So the SSD path stops at
"error" with a clear decoded message from v0.6.3's ATA error
decoder.

What this module adds
---------------------
When an SSD's secure_erase fails with the freeze signature, the
orchestrator calls `register_freeze(state, ...)` to track the
drive on DaemonState. The dashboard renders a dedicated remediation
panel for drives in that dict: a checklist of paths that bypass
libata's freeze (USB-SATA enclosure, Dell Lifecycle Controller,
vendor tools, SED PSID reset), ending in "mark as unrecoverable /
destroy physically" if nothing else worked.

Two operator actions clear state:
  - "I tried something, retest" — bumps retry_count, waits for the
    next pipeline attempt on this serial. If the next attempt also
    fails with the freeze signature, the state is promoted to
    `DESTRUCTION_RECOMMENDED` (the panel changes tone).
  - "Mark as unrecoverable" — drive gets a sticky F classification
    (via the normal F path, not via this module) and the entry is
    cleared.

This module is a pure state container + the step list. Orchestrator
integration lives in `daemon/orchestrator.py`; UI rendering in the
drive-detail template + new routes in `web/routes.py`.

Design choice: state is in-memory (`DaemonState.frozen_remediation`).
Persistent storage would require a DB migration + doesn't buy much:
a daemon restart implicitly resets remediation state, which is the
right behavior (every restart is a chance for the udev-probe race
to play out differently). Long-term, a drive that consistently
refuses on restart should end up as an F via the normal pipeline
after enough retries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum


class FrozenRemediationStatus(str, Enum):
    """Panel tone. Derived from retry_count but materialized as an
    enum so the template can branch cleanly without repeating the
    threshold logic."""

    # First time the freeze signature was caught on this drive in
    # this daemon lifetime. Full checklist shown; operator picks a
    # remediation path.
    NEEDS_ACTION = "needs_action"

    # Operator already clicked "retest" at least once and the drive
    # hit the freeze signature again. Checklist is still shown but
    # with an escalated tone ("this drive has refused twice; the
    # remediation paths below are worth another try, but physical
    # destruction is the fallback").
    RETRIED_STILL_FROZEN = "retried"


@dataclass
class RemediationStep:
    """One entry in the checklist. `kind` is a stable machine-readable
    key (for analytics / future integrations); `title` is the user-
    facing heading; `detail` is a paragraph of operator guidance."""

    kind: str
    title: str
    detail: str


# The checklist itself. Order matters — least-to-most invasive.
# Hardcoded today; if vendors / hosts demand per-drive-class variants
# later, this becomes a lookup on drive model.
REMEDIATION_STEPS: list[RemediationStep] = [
    RemediationStep(
        kind="usb_enclosure",
        title="Try the drive in a USB-SATA enclosure on any host",
        detail=(
            "USB-SATA bridges route ATA commands through their own firmware, "
            "which typically doesn't auto-issue SECURITY FREEZE LOCK on probe. "
            "Plug the drive into a USB enclosure, run `hdparm --user-master u "
            "--security-erase PASSWORD /dev/sdX` from any Linux box, then "
            "reinsert into DriveForge and retest."
        ),
    ),
    RemediationStep(
        kind="lifecycle_controller",
        title="Dell Lifecycle Controller / iDRAC secure-erase (Dell hosts)",
        detail=(
            "Works on R620/R720/R730-era Dell servers with iDRAC 7+. Reboot "
            "into Lifecycle Controller (F10 at POST), Hardware Configuration "
            "→ Services → Secure Erase. The controller's erase path runs "
            "out-of-band and doesn't use libata, so the freeze never happens."
        ),
    ),
    RemediationStep(
        kind="vendor_iso",
        title="Vendor secure-erase ISO (SeaTools, Samsung Magician, etc.)",
        detail=(
            "Boot the drive vendor's secure-erase bootable ISO (SeaTools for "
            "Seagate, Samsung Magician ISO, Intel SSD Toolbox USB). Those tools "
            "run outside Linux and bypass libata entirely. Good fallback for "
            "non-Dell hosts where Lifecycle Controller isn't an option."
        ),
    ),
    RemediationStep(
        kind="psid_reset",
        title="PSID factory reset (Self-Encrypting Drives only)",
        detail=(
            "If the drive is a SED with a PSID printed on its label, run "
            "`sedutil-cli --PSIDrevert <PSID> /dev/sdX` to cryptographically "
            "revert the drive to factory. This destroys the media-encryption "
            "key, making all data unrecoverable — no sector overwrite needed. "
            "DriveForge will rediscover the drive as blank on the next insert."
        ),
    ),
    RemediationStep(
        kind="destroy",
        title="Mark as unrecoverable and destroy physically",
        detail=(
            "If none of the above clear the freeze, the drive is not "
            "sanitizable via any software path. NIST 800-88 permits physical "
            "destruction (shred, degauss, drill-through-the-platters-and-"
            "controller) as a Clear/Purge method. Use the \"Mark as "
            "unrecoverable\" button below to stamp an F grade so auto-enroll "
            "skips this serial on future inserts."
        ),
    ),
]


@dataclass
class FrozenRemediationState:
    """Per-drive remediation tracking. One entry per serial in
    `DaemonState.frozen_remediation`."""

    serial: str
    drive_model: str
    first_seen_at: datetime
    last_seen_at: datetime
    retry_count: int = 0
    status: FrozenRemediationStatus = FrozenRemediationStatus.NEEDS_ACTION

    @property
    def steps(self) -> list[RemediationStep]:
        """Expose the checklist as a property so the template can
        iterate without importing the module-level constant."""
        return REMEDIATION_STEPS

    @property
    def is_retry(self) -> bool:
        """Template helper — true when the operator already clicked
        retry at least once."""
        return self.retry_count > 0


def register_freeze(
    frozen_map: dict[str, FrozenRemediationState],
    *,
    serial: str,
    drive_model: str,
) -> FrozenRemediationState:
    """Called from the orchestrator when an SSD secure_erase fails
    with the libata-freeze signature. Adds a new entry OR bumps
    retry_count on an existing one.

    Returns the resulting FrozenRemediationState so the caller can
    log its current status.

    First call: status=NEEDS_ACTION, retry_count=0
    Second call (retry after operator clicked "retest"):
        status=RETRIED_STILL_FROZEN, retry_count=1
    """
    now = datetime.now(UTC)
    existing = frozen_map.get(serial)
    if existing is None:
        new_state = FrozenRemediationState(
            serial=serial,
            drive_model=drive_model,
            first_seen_at=now,
            last_seen_at=now,
        )
        frozen_map[serial] = new_state
        return new_state

    existing.last_seen_at = now
    existing.retry_count += 1
    existing.status = FrozenRemediationStatus.RETRIED_STILL_FROZEN
    # drive_model may differ between first and second sightings
    # (e.g. operator replaced the drive with a different one in the
    # same slot — unlikely but defensive). Keep the latest.
    existing.drive_model = drive_model
    return existing


def mark_retried(
    frozen_map: dict[str, FrozenRemediationState],
    serial: str,
) -> FrozenRemediationState | None:
    """Called from the `Retest` button handler. Does NOT clear the
    entry — that happens when the next secure_erase run either
    succeeds (orchestrator calls `clear`) OR fails with the freeze
    signature again (orchestrator calls `register_freeze` which
    bumps retry_count). This function's job is just to say "operator
    has done their part; we're waiting on the next pipeline
    attempt". Kept as a no-op placeholder for now — the retry is
    tracked by the next register_freeze call.

    Returns the existing entry unchanged (or None if absent).
    """
    return frozen_map.get(serial)


def clear(
    frozen_map: dict[str, FrozenRemediationState],
    serial: str,
) -> None:
    """Remove the serial from remediation tracking. Called by the
    orchestrator on successful pipeline completion OR by the
    "Mark as unrecoverable" handler after stamping F."""
    frozen_map.pop(serial, None)
