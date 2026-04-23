"""Password-locked drive remediation state + checklist.

v0.9.0+. Sibling to v0.6.9's `frozen_remediation` — same shape, same
operator UX pattern, different failure mode.

Problem class
-------------
A drive comes to DriveForge with a **user password already set** by
some prior host (laptop BIOS HDD password, vendor utility, previous
owner, iDRAC/Lifecycle Controller residue, etc.). The drive is in
ATA SECURITY LOCKED state: it refuses all read/write I/O until
`SECURITY UNLOCK <password>` succeeds. DriveForge's default password
("driveforge") doesn't unlock it, AND the v0.9.0 vendor-factory-
master-password auto-recovery path (in `core.erase`) also failed —
either because the drive's vendor isn't in our table, or the master
password has been changed from factory (hdparm -I showed
revision != 65534).

What this module adds
---------------------
When `core.erase`'s preflight hits this failure mode, the orchestrator
calls `register_locked(state, ...)` to track the drive on
`DaemonState.password_locked`. The drive-detail page renders a
dedicated remediation panel (template analogous to v0.6.9's frozen-
SSD panel) with three operator actions:

  1. **PSID factory reset** (SEDs only) — DriveForge displays the
     steps for running `sedutil-cli --PSIDrevert <PSID> /dev/sdX`
     manually. Operator runs it, reinserts the drive, pipeline
     restarts clean. Same pattern as v0.6.9 frozen-SSD panel's SED
     option. Not yet auto-run by DriveForge (sedutil-cli isn't in
     Debian's main repos; post-v0.9.0 work to install + integrate).

  2. **Manual password attempt** — free-text password field + "Try
     unlock" button. Server-side runs `hdparm --security-disable
     <password> /dev/sdX` under the hood. Success → drive is
     security-disabled + pipeline continues from CLEAN state. Failure
     → attempts counter climbs; panel shows a warning that drives
     typically lock out permanently after ~5 wrong tries.

  3. **Mark as unrecoverable** — stamps an F grade on the latest
     TestRun for this serial so auto-enroll skip-logic takes over on
     future inserts. Clears the remediation entry.

Design mirrors `frozen_remediation.py`:
  - In-memory state only (`DaemonState.password_locked`). No DB
    migration needed for what's fundamentally operator-workflow
    UI state.
  - Per-drive retry_count + status fields; panel tone escalates
    when retry_count > 0 (operator tried manual password but drive
    is still locked — guidance shifts toward destruction).
  - `register_locked` / `clear` / `mark_retried` / `increment_attempt`
    helpers with the same naming as the frozen-remediation module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum


class PasswordLockedStatus(str, Enum):
    """Panel tone. Like the frozen-SSD analog: starts at
    NEEDS_ACTION, escalates after operator tries + fails a manual
    password unlock."""

    NEEDS_ACTION = "needs_action"          # First sighting; full checklist shown
    RETRIED_STILL_LOCKED = "retried"       # Operator tried manual password + failed


@dataclass
class RemediationStep:
    """One entry in the panel's structured checklist."""

    kind: str
    title: str
    detail: str


# The baked-in checklist. Order matters — least-to-most destructive.
# Hardcoded today; per-vendor/per-model variants would be a post-
# v0.9.0 enhancement if a specific drive class needed different copy.
REMEDIATION_STEPS: list[RemediationStep] = [
    RemediationStep(
        kind="psid_revert",
        title="PSID factory reset (Self-Encrypting Drives only)",
        detail=(
            "If the drive is a Self-Encrypting Drive (SED) with a PSID "
            "printed on its label, a PSID revert cryptographically wipes "
            "the media-encryption key, rendering all data unrecoverable "
            "AND clearing the password state. From any Linux box with "
            "sedutil-cli installed, run: "
            "`sudo sedutil-cli --PSIDrevert <PSID-from-label> /dev/sdX`. "
            "The drive comes back factory-blank. Reinsert into DriveForge "
            "and hit Retest below; preflight will see security-DISABLED "
            "and the pipeline restarts normally."
        ),
    ),
    RemediationStep(
        kind="manual_password",
        title="Try a known password (manual unlock)",
        detail=(
            "If you know or suspect the password that was set (common "
            "sources: laptop BIOS HDD password, vendor-utility-set "
            "password, prior operator default), enter it in the field "
            "below and click Try unlock. DriveForge runs "
            "`hdparm --security-disable` under the hood. WARNING: ATA "
            "drives typically permanently lock out after ~5 wrong "
            "attempts. Only guess if you have a real suspicion about "
            "the password."
        ),
    ),
    RemediationStep(
        kind="vendor_recovery_iso",
        title="Vendor recovery utility",
        detail=(
            "Some vendors ship recovery utilities (WD Drive Utilities, "
            "Seagate SeaTools, Samsung Magician, etc.) that can issue "
            "SECURITY DISABLE from a bootable ISO. Boot the drive in a "
            "system that can run the vendor tool, run the tool's "
            "password-disable or secure-erase function, then reinsert "
            "into DriveForge."
        ),
    ),
    RemediationStep(
        kind="destroy",
        title="Mark as unrecoverable and destroy physically",
        detail=(
            "If no path above works, the drive is not sanitizable via "
            "software. Physical destruction (shred, degauss, drill-"
            "through-platters-and-controller) is NIST 800-88's accepted "
            "Clear/Purge method for unrecoverable drives. Use the "
            "Mark as unrecoverable button below to stamp an F grade so "
            "auto-enroll skips this serial on future inserts."
        ),
    ),
]


@dataclass
class PasswordLockedState:
    """Per-drive lock-remediation tracking. One entry per serial in
    `DaemonState.password_locked`."""

    serial: str
    drive_model: str
    first_seen_at: datetime
    last_seen_at: datetime
    # Count of manual-password-unlock attempts the operator has made
    # through the UI. Surfaced in the panel as a warning ("you've tried
    # N/5; drive may lock permanently after 5 wrong tries").
    manual_attempts: int = 0
    # Count of times this drive has been re-registered (preflight hit
    # the locked state again after a Retest). Each bump escalates the
    # panel tone.
    retry_count: int = 0
    status: PasswordLockedStatus = PasswordLockedStatus.NEEDS_ACTION
    # Last manual-password-attempt result message. Surfaces "wrong
    # password" vs "drive is now in LOCKED+pending state" vs network
    # / plumbing errors. None when no attempt has been made yet.
    last_attempt_note: str | None = None

    @property
    def steps(self) -> list[RemediationStep]:
        return REMEDIATION_STEPS

    @property
    def attempts_remaining_estimate(self) -> int:
        """Conservative estimate of manual tries left before the drive
        permanently locks out. ATA spec is typically 5; vendor firmware
        varies. Render as ~this many in the panel to keep operators
        from burning their last tries on hunches."""
        return max(0, 5 - self.manual_attempts)


def register_locked(
    locked_map: dict[str, PasswordLockedState],
    *,
    serial: str,
    drive_model: str,
) -> PasswordLockedState:
    """Called by the orchestrator when secure_erase preflight fails
    with the security-locked pattern. Adds a new entry OR bumps
    retry_count on an existing one (escalates panel tone).

    Mirrors the `frozen_remediation.register_freeze` contract.
    """
    now = datetime.now(UTC)
    existing = locked_map.get(serial)
    if existing is None:
        new_state = PasswordLockedState(
            serial=serial,
            drive_model=drive_model,
            first_seen_at=now,
            last_seen_at=now,
        )
        locked_map[serial] = new_state
        return new_state
    existing.last_seen_at = now
    existing.retry_count += 1
    existing.status = PasswordLockedStatus.RETRIED_STILL_LOCKED
    existing.drive_model = drive_model
    return existing


def record_manual_attempt(
    locked_map: dict[str, PasswordLockedState],
    serial: str,
    *,
    ok: bool,
    note: str,
) -> PasswordLockedState | None:
    """Record a manual-password-unlock attempt made through the UI.
    Bumps `manual_attempts` + stores the result note. Caller is
    responsible for clearing the entry via `clear()` on success
    (because a successful manual unlock means pipeline should
    re-run, and remediation state is no longer relevant)."""
    existing = locked_map.get(serial)
    if existing is None:
        return None
    existing.manual_attempts += 1
    existing.last_attempt_note = note
    existing.last_seen_at = datetime.now(UTC)
    # Note: we don't auto-clear on ok=True here. The orchestrator
    # decides when to clear based on the next pipeline run's
    # success. Keeping the entry until proven-clean avoids races
    # where operator clicks Retest before pipeline completion.
    return existing


def mark_retried(
    locked_map: dict[str, PasswordLockedState],
    serial: str,
) -> PasswordLockedState | None:
    """Operator clicked Retest. Mirrors
    frozen_remediation.mark_retried — doesn't bump counts itself;
    the NEXT `register_locked` call (if the drive is still locked
    after operator remediation) does that."""
    return locked_map.get(serial)


def clear(
    locked_map: dict[str, PasswordLockedState],
    serial: str,
) -> None:
    """Remove the serial from tracking. Called on successful pipeline
    completion (drive was unlocked by operator + regraded clean) OR
    by the Mark-Unrecoverable handler after F is stamped on the
    latest TestRun."""
    locked_map.pop(serial, None)
