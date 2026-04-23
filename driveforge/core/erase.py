"""Secure erase dispatch.

One entry point — `secure_erase(drive)` — picks the right mechanism based on
transport:

- SATA: SAT passthrough — ATA SECURITY ERASE UNIT wrapped in
        ATA-PASS-THROUGH(16) SCSI CDB, issued via `sg_raw`. Replaces
        `hdparm --security-erase` as of v0.3.0; the legacy hdparm
        path uses `HDIO_DRIVE_TASKFILE`, which modern Debian kernels
        no longer provide on SAS-attached drives (CONFIG_IDE_TASK_IOCTL
        error). See `driveforge.core.sat_passthru` for the SAT details.
- SAS:  `sg_format --format`
- NVMe: `nvme format -s 1`

All are destructive. The orchestrator is responsible for confirming intent
before calling this.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

from driveforge.core import sat_passthru
from driveforge.core.drive import Drive, Transport
from driveforge.core.process import run
from driveforge.core.timing import capacity_timeout

logger = logging.getLogger(__name__)


class EraseError(RuntimeError):
    pass


# Parses `hdparm -I` security stanza. Examples:
#   "2min for SECURITY ERASE UNIT. 2min for ENHANCED SECURITY ERASE UNIT."
#   "128min for SECURITY ERASE UNIT. 90min for ENHANCED SECURITY ERASE UNIT."
# Some drives report nothing, some report hours — bail out and fall back to
# a capacity heuristic if this doesn't match.
_SATA_SE_TIME_RE = re.compile(
    r"(\d+)\s*min\s+for\s+SECURITY\s+ERASE\s+UNIT", re.IGNORECASE
)


def _sata_estimated_seconds(device: str) -> int | None:
    try:
        r = run(["hdparm", "-I", device], timeout=10)
    except Exception:  # noqa: BLE001
        return None
    if not r.ok:
        return None
    m = _SATA_SE_TIME_RE.search(r.stdout)
    if not m:
        return None
    return max(60, int(m.group(1)) * 60)


def estimate_erase_seconds(drive: Drive) -> int | None:
    """Return a best-guess wall-clock for the secure-erase phase.

    Used by the orchestrator to drive a time-based progress bar for a phase
    that emits no native progress. Returns None when we genuinely can't
    estimate — caller should show an indeterminate/busy state instead of a
    misleading 0%.
    """
    if drive.transport == Transport.SATA:
        # Ask the drive how long SECURITY ERASE UNIT is expected to take.
        est = _sata_estimated_seconds(drive.device_path)
        if est is not None:
            return est
        # Fall through to capacity heuristic below.
    if drive.transport == Transport.NVME:
        # NVMe format is typically a few seconds even on multi-TB drives
        # (crypto-erase), but give ourselves headroom.
        return 60
    # SAS sg_format and the SATA fallback: estimate from capacity. These are
    # intentionally pessimistic so the bar under-reports rather than lying
    # past 100%. Real sg_format on a 300 GB SAS runs 15-60 min.
    if drive.capacity_bytes:
        gb = drive.capacity_bytes / 1_000_000_000
        # 20 min per 100 GB, clamped to [5 min, 6 h]
        seconds = int((gb / 100) * 20 * 60)
        return max(5 * 60, min(seconds, 6 * 3600))
    return None


class SataSecurityState(str, Enum):
    """Possible SATA security states visible via `hdparm -I`."""

    CLEAN = "clean"       # security not enabled — fresh drive, ready to erase
    ENABLED = "enabled"   # password set but drive is not locked — can DISABLE with right password
    LOCKED = "locked"     # password set AND drive locked (post-power-cycle state)
    FROZEN = "frozen"     # BIOS issued SECURITY FREEZE LOCK; cannot unlock via software
    UNKNOWN = "unknown"   # hdparm -I failed or output was unparseable


def _parse_sata_security_state(hdparm_i_output: str) -> SataSecurityState:
    """Extract the drive's security state from `hdparm -I` stdout.

    hdparm prints a `Security:` stanza with lines like:
        	not	enabled
        	not	locked
        	not	frozen
    (tab-separated). Parsing via substring match is brittle because
    'not enabled' and 'enabled' both contain the word 'enabled'; we use
    the tab prefix as a cheap but reliable anchor.
    """
    out = hdparm_i_output.lower()
    is_frozen = "\tfrozen" in out and "not\tfrozen" not in out
    is_locked = "\tlocked" in out and "not\tlocked" not in out
    is_enabled = "\tenabled" in out and "not\tenabled" not in out

    # Order matters: frozen beats locked beats enabled. A frozen drive
    # will typically also report "enabled" in hdparm's output; what
    # matters operationally is that we can't do anything security-side
    # until the BIOS-induced freeze is cleared.
    if is_frozen:
        return SataSecurityState.FROZEN
    if is_locked:
        return SataSecurityState.LOCKED
    if is_enabled:
        return SataSecurityState.ENABLED
    return SataSecurityState.CLEAN


def _probe_sata_security_state(device: str) -> SataSecurityState:
    """Run `hdparm -I <device>` and return the parsed security state.

    hdparm -I is read-only — it uses HDIO_GET_IDENTITY which still
    works on modern kernels (it's the HDIO_DRIVE_TASKFILE ioctl for
    destructive commands that was removed). So we keep using hdparm
    for this probe even though the actual erase went through SAT
    passthrough in v0.3.0+.
    """
    result = run(["hdparm", "-I", device], timeout=10)
    if not result.ok:
        logger.warning(
            "hdparm -I failed on %s (rc=%d): %s",
            device, result.returncode, (result.stderr or "").strip(),
        )
        return SataSecurityState.UNKNOWN
    return _parse_sata_security_state(result.stdout or "")


def ensure_clean_security_state(drive: Drive) -> None:
    """Self-healing pre-flight for the SATA secure_erase phase (v0.5.0+).

    Before kicking off SET PASSWORD → PREPARE → ERASE UNIT, verify the
    drive is in a sane security state. Auto-resolve what we can
    (drives still enabled/locked from a previous interrupted run —
    disable the password, start clean), refuse with a clear user-
    facing explanation on what we can't (frozen BIOS, unknown
    password set by another tool). Net effect: no code path left
    where the operator has to SSH in and hand-run hdparm.

    No-op for SAS and NVMe — they don't have ATA security state.
    SAS drives behind a SAS HBA sometimes report as "sas" via lsblk
    but speak ATA; we refine via smartctl first so we run the right
    pre-flight.

    Raises EraseError on any genuinely-unrecoverable state. The error
    message is written for the operator, not the developer — it
    includes what state the drive is in, why we can't handle it
    automatically, and what they should do next.
    """
    effective = drive.transport
    if effective == Transport.SAS:
        from driveforge.core.drive import detect_true_transport
        refined = detect_true_transport(drive.device_path)
        if refined in (Transport.SATA, Transport.SAS, Transport.NVME):
            effective = refined

    if effective != Transport.SATA:
        # SAS and NVMe have no ATA security state — no pre-flight needed.
        return

    state = _probe_sata_security_state(drive.device_path)
    logger.info(
        "secure_erase preflight: %s (%s) reports security state = %s",
        drive.device_path, drive.serial, state.value,
    )

    if state == SataSecurityState.CLEAN:
        return  # Nothing to do.

    if state == SataSecurityState.UNKNOWN:
        raise EraseError(
            f"preflight: could not read security state from {drive.device_path} "
            f"(hdparm -I failed). Drive may be unresponsive, cabling may be "
            f"loose, or the HBA may have lost the drive. Check "
            f"`dmesg | tail` and `lsblk` for the drive's current state."
        )

    if state == SataSecurityState.FROZEN:
        raise EraseError(
            f"preflight: drive {drive.device_path} is in FROZEN security state. "
            f"The system BIOS issued SECURITY FREEZE LOCK during POST, which "
            f"cannot be undone via software. Options: (1) reboot into a BIOS "
            f"that has 'security freeze lock' disabled, (2) on some chassis, "
            f"hot-remove + re-insert the drive (power cycles just the drive, "
            f"clearing the freeze), (3) replace the drive."
        )

    # state is LOCKED or ENABLED. Both mean "our previous run (or someone
    # else) set a security password." Try to clear it with our known
    # throwaway password; surface a clear error if that fails (drive has
    # a user-set password we don't know).

    if state == SataSecurityState.LOCKED:
        logger.info(
            "preflight: attempting SAT unlock on %s with default password",
            drive.device_path,
        )
        try:
            sat_passthru.security_unlock(
                drive.device_path, owner=drive.serial,
            )
        except sat_passthru.SatPassthruError as exc:
            # v0.9.0+: default-password unlock failed → try vendor-factory
            # master-password SECURITY ERASE ENHANCED as a last-ditch
            # recovery BEFORE failing over to operator remediation. This
            # auto-recovers the common "laptop BIOS password left on a
            # WD drive" case without requiring operator action. Safe
            # because:
            #   (a) we only try vendors we recognize (prefix-match),
            #   (b) we precheck `hdparm -I` master-revision = 65534
            #       (factory default) before attempting — if the master
            #       was changed, we'd burn a lockout strike for nothing
            #       and skip instead,
            #   (c) enhanced erase IS the intended outcome for refurb
            #       sanitization — drive comes back clean + unlocked.
            master_ok, master_msg = _try_factory_master_erase(
                drive.device_path, drive.model, owner=drive.serial,
            )
            if master_ok:
                logger.info(
                    "preflight: recovered security-locked drive %s via vendor-"
                    "factory-master ENHANCED erase. Pipeline continuing from "
                    "CLEAN state.",
                    drive.device_path,
                )
                # After enhanced-erase, drive is in CLEAN state (security
                # disabled + data wiped). Re-probe to confirm and return
                # to the caller. The pipeline's next step (normal
                # secure_erase) will effectively be a no-op on an
                # already-wiped drive, which is fine.
                return
            # Factory master attempt also failed — log what we tried
            # + raise the EraseError with the v0.9.0 pattern-matchable
            # text so `is_security_locked_pattern()` triggers and the
            # orchestrator routes to the remediation panel.
            logger.info(
                "preflight: factory-master auto-recovery did not help %s: %s",
                drive.device_path, master_msg,
            )
            raise EraseError(
                f"preflight: drive {drive.device_path} is security-locked with "
                f"an unknown password. DriveForge's default password "
                f"('{sat_passthru.DEFAULT_PASSWORD}') did not unlock it, and "
                f"the vendor-factory-master SECURITY ERASE ENHANCED recovery "
                f"path also failed ({master_msg}). Operator remediation "
                f"required — see the drive-detail page for the PSID-revert / "
                f"manual-password / mark-unrecoverable options. "
                f"Underlying unlock error: {exc}"
            ) from exc
        # Unlock succeeded → drive is now enabled-but-not-locked. Fall through.
        logger.info("preflight: SAT unlock succeeded on %s", drive.device_path)

    # state is ENABLED (either originally, or after unlock).
    logger.info(
        "preflight: attempting SAT disable-password on %s with default password",
        drive.device_path,
    )
    try:
        sat_passthru.security_disable_password(
            drive.device_path, owner=drive.serial,
        )
    except sat_passthru.SatPassthruError as exc:
        raise EraseError(
            f"preflight: drive {drive.device_path} has security enabled but "
            f"DriveForge could not disable it with its default password "
            f"('{sat_passthru.DEFAULT_PASSWORD}'). This typically means the "
            f"password was set by another tool. See the locked-state recovery "
            f"options in the docs (/hardware/known-issues). Underlying "
            f"error: {exc}"
        ) from exc

    logger.info(
        "preflight: %s is now in CLEAN security state, ready for fresh erase",
        drive.device_path,
    )


def _sata_secure_erase(
    device: str,
    password: str = sat_passthru.DEFAULT_PASSWORD,
    *,
    owner: str | None = None,
    capacity_bytes: int | None = None,
    on_status=None,
) -> None:
    """SAT-passthrough secure erase for SATA drives with hdparm fallback.

    Primary path: SAT passthrough via `sg_raw` + ATA-PASS-THROUGH(16)
    (v0.3.0+). Works on modern Debian kernels where the old hdparm
    `HDIO_DRIVE_TASKFILE` ioctl is gone.

    Fallback path (v0.6.3+): if the SAT `SECURITY ERASE UNIT`
    command aborts (`SatPassthruError` containing "SECURITY ERASE
    UNIT" + "Aborted"), retry via `hdparm --user-master u
    --security-erase`. We discovered on a ST4000NM0033 on JT's R720
    (2026-04-21) that some drives' SAT translation layer refuses our
    CDB for ERASE UNIT specifically while happily accepting the
    identical ATA command issued directly via hdparm. Rather than
    grade the drive F on a transport-layer refusal, v0.6.3 retries
    via the native-ATA hdparm path. Same password, same drive-side
    semantics, different kernel path.
    """
    # Timeout preference: (1) drive's own hdparm-announced estimate × 1.5 if
    # present — vendor firmware knows the drive better than our blanket
    # capacity model — (2) capacity-based fallback otherwise. hdparm -I
    # uses HDIO_GET_IDENTITY which still works on modern kernels (it's
    # only the legacy IDE *task* ioctl that was removed); we keep using
    # it for the time estimate even though the actual erase has moved to
    # SAT passthrough. No arbitrary upper cap — if an 8 TB drive needs
    # 40 h, give it 40 h. The operator can abort from the dashboard if
    # something's genuinely hung.
    est = _sata_estimated_seconds(device)
    if est is not None:
        timeout_s = max(3600, int(est * 1.5))
    else:
        timeout_s = capacity_timeout(capacity_bytes, passes=1)
    def _notify(msg: str) -> None:
        """Best-effort status callback. Runs in the executor thread on
        the orchestrator's behalf; never raised back to the erase
        logic even if the callback bugs out."""
        if on_status is None:
            return
        try:
            on_status(msg)
        except Exception:  # noqa: BLE001
            logger.exception("on_status callback raised (ignored)")

    try:
        _notify("SAT passthrough secure erase starting")
        sat_passthru.sat_secure_erase(
            device, password=password, timeout_s=timeout_s, owner=owner
        )
        _notify("SAT passthrough secure erase completed")
        return
    except sat_passthru.SatPassthruError as exc:
        if _is_sat_erase_unit_abort(str(exc)):
            logger.warning(
                "secure_erase: SAT ERASE UNIT aborted on %s — falling back to "
                "hdparm native-ATA path (v0.6.3+). Underlying: %s",
                device, exc,
            )
            _notify(
                "SAT ERASE UNIT aborted — falling back to hdparm native-ATA"
            )
            try:
                _sata_secure_erase_hdparm(
                    device,
                    password=password,
                    timeout_s=timeout_s,
                    owner=owner,
                    on_status=_notify,
                )
                logger.info(
                    "secure_erase: hdparm fallback succeeded on %s "
                    "(SAT path refused; hdparm accepted)",
                    device,
                )
                _notify("hdparm fallback secure erase completed")
                return
            except EraseError as hdparm_exc:
                # Both paths refused — this is a legitimate drive-refusal.
                # Surface both errors so the decoder has full context.
                raise EraseError(
                    f"Both SAT and hdparm secure-erase refused. "
                    f"SAT: {exc}. hdparm: {hdparm_exc}"
                ) from hdparm_exc
        # Non-ABRT failure from SAT — re-wrap as EraseError without
        # fallback (hdparm wouldn't help with transport-level failures
        # like "sg_raw returned non-zero before reaching the drive").
        raise EraseError(str(exc)) from exc


def _is_sat_erase_unit_abort(err_text: str) -> bool:
    """True iff the SAT passthrough error looks like an ERASE UNIT
    ABRT — the specific case v0.6.3 hdparm-fallback handles.

    We match on substrings rather than parsing the full error
    because sg_raw's error format varies slightly across versions
    and we'd rather err on the side of triggering the fallback
    (hdparm failing again is cheap) than missing it (operator sees
    F grade they shouldn't).
    """
    t = (err_text or "").lower()
    if "security erase unit" not in t:
        return False
    return (
        "aborted command" in t
        or "aborted" in t
        or "error=0x4" in t
        or "check condition" in t
    )


def is_libata_freeze_pattern(err_text: str) -> bool:
    """True iff the error is the "both SAT and hdparm refused
    SECURITY ERASE UNIT with ABRT" signature produced by
    `_sata_secure_erase` when both paths fail (v0.6.3+).

    This is the textbook libata-auto-freeze case — the drive
    isn't broken, the kernel's libata driver just issued
    SECURITY FREEZE LOCK during udev probe, and no amount of
    software retry on this host will get the drive to accept
    the command. v0.6.7+ uses this signal to decide whether
    an HDD can fall back to badblocks-only sanitization
    (safe — 4-pattern overwrite = NIST 800-88 Clear for
    magnetic media) vs. failing the run outright.

    SSDs hitting this pattern should NOT use the fallback —
    wear leveling means logical-sector overwrite doesn't
    necessarily rewrite NAND. Caller must gate on drive type.
    """
    t = (err_text or "").lower()
    return (
        "both sat" in t
        and "hdparm" in t
        and ("abrt" in t or "aborted" in t or "refused" in t)
    )


def is_security_locked_pattern(err_text: str) -> bool:
    """True iff the preflight failed because the drive is in SECURITY
    LOCKED state with a password DriveForge doesn't know.

    Distinct from libata-freeze:
      - Freeze: drive refuses SECURITY ERASE UNIT specifically due to
        libata's in-kernel freeze; drive is otherwise I/O-capable.
      - Locked: drive refuses ALL I/O (including reads/writes) until
        SECURITY UNLOCK with the correct password; set by a prior
        host (laptop BIOS, vendor utility, previous owner, etc.).

    v0.9.0+ routes drives matching this pattern to the password-locked
    remediation panel on the drive-detail page (see
    `core.password_locked_remediation`). Operator then picks a
    recovery path: PSID revert (SEDs), manual-password attempt, or
    mark-as-unrecoverable.

    Match signal: the specific error string produced by `secure_erase`
    preflight's LOCKED-branch raise, after `_try_factory_master_erase`
    has also failed. Keep this match string stable if the error copy
    changes — the orchestrator + template branch on it.
    """
    t = (err_text or "").lower()
    return (
        "security-locked" in t
        and ("unknown password" in t or "did not unlock" in t)
    )


# v0.9.0+ Vendor factory master-password table. When a drive reports
# `hdparm -I` master-revision = 65534 (0xFFFE, meaning master password
# is at factory default) AND the drive is security-locked by some
# user-set password, the factory master password can usually still
# issue SECURITY ERASE UNIT (data wiped — which is what we want for
# refurb sanitization anyway). We match on the drive's model-string
# prefix (we already know which vendor by the time we get here).
#
# Blast radius: each attempt uses up one of the drive's ~5-strike
# internal lockout counter. We only try ONE vendor default per drive,
# keyed on the vendor prefix, so we burn at most one counter slot.
# If the attempt fails, we fall through to operator remediation.
_VENDOR_FACTORY_MASTER_PASSWORDS: tuple[tuple[str, str], ...] = (
    # Western Digital — "WDC" repeated to fill 32 bytes is the most
    # common factory master for WD HDDs of the 2010-2018 era. The
    # string is "WDC" × 10 + "WD" = 32 chars.
    ("WDC ",      "WDCWDCWDCWDCWDCWDCWDCWDCWDCWDCWD"),
    ("WD ",       "WDCWDCWDCWDCWDCWDCWDCWDCWDCWDCWD"),
    # Seagate — 32 null bytes is the documented factory default for
    # most Barracuda/Exos/Constellation models.
    ("Seagate",   "\x00" * 32),
    ("ST",        "\x00" * 32),  # model prefix pattern — ST3000DM, ST4000NM, etc.
    # Toshiba — 32 null bytes (some models use vendor-specific but
    # null-bytes is the documented default for MG/AL series).
    ("TOSHIBA",   "\x00" * 32),
    ("MG0",       "\x00" * 32),
    # HGST / Hitachi — 32 null bytes for Ultrastar family.
    ("HGST",      "\x00" * 32),
    ("HUS",       "\x00" * 32),
    ("HUH",       "\x00" * 32),
)


def _vendor_factory_master_for(model: str | None) -> str | None:
    """Pick the factory master password to try for this drive's
    vendor. Returns None when the model is unfamiliar — we DON'T
    guess across vendors because every wrong guess burns a counter
    slot.

    Longest-prefix-match semantics: "WDC " (4 chars) is checked
    before "WD " (3 chars) so `WDC WD10EZEX-...` routes to the WD
    entry, not something else that happens to start with "W".
    """
    if not model:
        return None
    # Longest-prefix-first iteration
    for prefix, password in sorted(
        _VENDOR_FACTORY_MASTER_PASSWORDS,
        key=lambda p: len(p[0]),
        reverse=True,
    ):
        if model.upper().startswith(prefix.upper()):
            return password
    return None


def _is_master_password_at_factory_default(hdparm_i_output: str) -> bool:
    """True iff hdparm -I reports `Master password revision code = 65534`
    (0xFFFE). That's the ATA-spec indicator for "master password has
    never been changed from factory." When the master is at default,
    the vendor factory master password will unlock / erase the drive.
    When the master has been CHANGED (revision != 65534), the vendor
    default won't work and we skip the auto-recovery path.
    """
    # Canonical line: "Master password revision code = 65534"
    # Older smartctl/hdparm variants may include extra whitespace or
    # a different case; use a permissive contains-check.
    t = hdparm_i_output or ""
    return "master password revision code = 65534" in t.lower()


def _try_factory_master_erase(
    device: str,
    model: str | None,
    *,
    owner: str | None = None,
) -> tuple[bool, str]:
    """v0.9.0+. When preflight hits LOCKED state and default-password
    unlock fails, try `hdparm --user-master m --security-erase-enhanced
    <vendor-factory-master>` as a last-ditch recovery before failing
    the drive over to operator remediation.

    Returns (ok, message). On success, the drive is wiped AND
    security-disabled by the ENHANCED SECURITY ERASE UNIT itself —
    no further cleanup needed; pipeline can restart from the top.
    On failure, the error string names which vendor master was tried
    so operator logs are actionable.

    Gated on two preconditions inside this function:
      1. Drive's vendor must match one of _VENDOR_FACTORY_MASTER_PASSWORDS
         (return early if not — don't guess across vendors).
      2. `hdparm -I` must show master-revision = 65534 (factory
         default). If the master was changed, the vendor default
         won't work and we'd burn a lockout strike for nothing.

    Called from the LOCKED branch of `_sata_secure_erase_preflight`
    (see `secure_erase()`'s preflight block).
    """
    master_pw = _vendor_factory_master_for(model)
    if master_pw is None:
        return (False, f"no known factory master password for vendor (model={model!r})")

    # Precondition 2: verify master-password revision is still at factory.
    try:
        probe = run(["hdparm", "-I", device], timeout=10)
    except Exception as exc:  # noqa: BLE001
        return (False, f"hdparm -I failed during master-revision probe: {exc}")
    if not _is_master_password_at_factory_default(probe.stdout):
        return (
            False,
            "master password is NOT at factory default (revision != 65534); "
            "vendor default won't work and we'd burn a lockout strike. skipping.",
        )

    logger.warning(
        "preflight: attempting vendor-factory-master SECURITY ERASE ENHANCED on %s "
        "(model=%r). This burns one of the drive's ~5 lockout strikes; fails gracefully "
        "if vendor default has been changed.",
        device, model,
    )

    # ENHANCED erase is destructive + disables security in one shot.
    # 118-min self-reported estimate for the WD1TB case; use capacity-
    # based timeout with generous headroom.
    timeout_s = 4 * 60 * 60  # 4 hours ceiling
    try:
        result = run(
            [
                "hdparm",
                "--user-master", "m",
                "--security-erase-enhanced",
                master_pw,
                device,
            ],
            timeout=timeout_s,
            owner=owner,
        )
    except Exception as exc:  # noqa: BLE001
        return (False, f"hdparm master-erase attempt crashed: {exc}")

    if result.returncode != 0:
        # Wrong password → ABRT. Don't retry with other vendor defaults;
        # we already matched the right vendor by model prefix.
        stderr = (result.stderr or "").strip()
        return (
            False,
            f"vendor-factory-master SECURITY ERASE ENHANCED failed on {device}: "
            f"rc={result.returncode} stderr={stderr!r}. The factory master may "
            f"have been changed on this drive; operator remediation required.",
        )

    logger.warning(
        "preflight: vendor-factory-master SECURITY ERASE ENHANCED SUCCEEDED on %s "
        "— drive is now security-disabled and data-wiped.",
        device,
    )
    return (True, "vendor-factory-master erase succeeded; drive is clean")


def _sata_secure_erase_hdparm(
    device: str,
    *,
    password: str,
    timeout_s: int,
    owner: str | None,
    on_status=None,
) -> None:
    """Native-ATA secure-erase via hdparm. The v0.6.3 fallback path for
    drives that refuse SAT passthrough ERASE UNIT.

    Two-command sequence (same drive-side flow as SAT, different
    kernel path to get there):
      1. `hdparm --user-master u --security-set-pass PW DEVICE`
      2. `hdparm --user-master u --security-erase PW DEVICE`

    Blocks until the erase completes (hdparm's own wait) or the
    timeout fires. `hdparm --security-erase` returns only after the
    drive reports the erase finished; for HDDs that can be many hours.
    Same `owner` mechanism as the SAT path so the kill-on-abort
    machinery works consistently.
    """
    def _notify(msg: str) -> None:
        if on_status is None:
            return
        try:
            on_status(msg)
        except Exception:  # noqa: BLE001
            logger.exception("on_status callback raised (ignored)")

    # Step 1: set password. If the drive's already in enabled state
    # (because we're retrying after SAT SET PASSWORD partially worked),
    # hdparm returns an error we can ignore — the important thing is
    # that the password is set and known to us.
    _notify("hdparm: setting security password")
    set_argv = [
        "hdparm", "--user-master", "u",
        "--security-set-pass", password, device,
    ]
    r = run(set_argv, timeout=60, owner=owner)
    if not r.ok and "already" not in (r.stderr or "").lower():
        # Genuine failure (not just "password already set"). Log but
        # proceed — if the password is wrong, the erase step will
        # fail with a distinctive error that propagates up.
        logger.warning(
            "hdparm --security-set-pass failed on %s (rc=%d): %s",
            device, r.returncode, (r.stderr or "").strip(),
        )

    # Step 2: the actual erase. hdparm handles SECURITY ERASE PREPARE
    # + SECURITY ERASE UNIT internally in the right order; we don't
    # need to split them. Blocks until the drive reports done.
    _notify("hdparm: issuing SECURITY ERASE (blocking until drive reports done)")
    erase_argv = [
        "hdparm", "--user-master", "u",
        "--security-erase", password, device,
    ]
    r = run(erase_argv, timeout=timeout_s + 60, owner=owner)
    if not r.ok:
        raise EraseError(
            f"hdparm --security-erase failed on {device} "
            f"(rc={r.returncode}): {(r.stderr or r.stdout or '').strip() or 'non-zero exit'}"
        )


def wait_for_prior_erase_completion(
    drive: Drive,
    *,
    poll_interval_s: int = 60,
    max_wait_s: int = 12 * 3600,
    progress_callback=None,
) -> bool:
    """Wait for a drive's in-progress secure-erase to finish (v0.6.3+).

    Use case — Case B on re-insert: if a drive was pulled mid-erase
    and its firmware resumed the erase on power-up, the drive is
    locked + unresponsive to unlock commands until the internal erase
    finishes. This function polls `hdparm -I` every `poll_interval_s`
    seconds until the drive returns to CLEAN (erase completed, password
    auto-cleared), or `max_wait_s` elapses.

    `progress_callback(elapsed_s, state)` is called on every poll so
    the dashboard can update the drive card sub-label with elapsed
    time + current state. Pass None to skip progress reporting
    (e.g. tests).

    Returns True if the drive became CLEAN within the deadline,
    False if the deadline hit first. Caller decides next step —
    typically "proceed with pipeline" on True, "grade F with
    'erase-never-completed' reason" on False.

    Note: we intentionally do NOT try to accelerate the erase or
    interrupt it. Once a drive starts SECURITY ERASE UNIT, it
    completes on its own schedule. The polling cadence is just to
    detect when it's safe to proceed — there's no mechanism that
    makes it finish faster.
    """
    import time

    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed > max_wait_s:
            logger.warning(
                "wait_for_prior_erase_completion: %s (%s) did not complete "
                "within %d s deadline; giving up",
                drive.device_path, drive.serial, max_wait_s,
            )
            return False
        state = _probe_sata_security_state(drive.device_path)
        if progress_callback is not None:
            try:
                progress_callback(elapsed, state)
            except Exception:  # noqa: BLE001
                # Never let a callback bug crash the wait loop — we need
                # to keep polling regardless of whether the UI update lands.
                logger.exception("progress_callback raised during wait loop")
        if state == SataSecurityState.CLEAN:
            logger.info(
                "wait_for_prior_erase_completion: %s (%s) returned to CLEAN "
                "after %.0f min — erase completed",
                drive.device_path, drive.serial, elapsed / 60,
            )
            return True
        if state == SataSecurityState.UNKNOWN:
            # hdparm -I failed — drive may have been pulled again, or the
            # bus is having problems. Keep polling; if it stays UNKNOWN
            # we'll eventually hit the max_wait timeout.
            logger.debug(
                "wait_for_prior_erase_completion: %s state=UNKNOWN at %.0f s, "
                "continuing poll",
                drive.device_path, elapsed,
            )
        time.sleep(poll_interval_s)


def _sas_secure_erase(device: str, *, owner: str | None = None, capacity_bytes: int | None = None) -> None:
    # sg_format FORMAT UNIT is one full-disk overwrite in firmware — scales
    # linearly with capacity just like SATA SE. The old flat 12 h cap was
    # fine for 300 GB-1 TB drives but would silently kill 4 TB+ sg_format
    # jobs mid-flight, and mid-flight sg_format abort corrupts the drive
    # (requires manual recovery). Use the same capacity model as SATA.
    timeout_s = capacity_timeout(capacity_bytes, passes=1)
    r = run(["sg_format", "--format", device], timeout=timeout_s, owner=owner)
    if not r.ok:
        raise EraseError(f"sg_format failed on {device}: {r.stderr}")


def _nvme_format(device: str, *, owner: str | None = None) -> None:
    # -s 1 = user-data erase; -f = force, suppress prompts. NVMe format is
    # a crypto-erase — completes in seconds to minutes even on multi-TB
    # drives, so the flat 1 h cap is fine for any size we'd plausibly see.
    r = run(
        ["nvme", "format", "-s", "1", "-f", device],
        timeout=60 * 60,
        owner=owner,
    )
    if not r.ok:
        raise EraseError(f"nvme format failed on {device}: {r.stderr}")


def secure_erase(drive: Drive, *, on_status=None) -> None:
    """Dispatch to the right erase path based on transport.

    For drives classified as SAS by lsblk, re-probe via smartctl first —
    SATA drives attached to SAS HBAs show up as tran=sas in lsblk but
    actually speak ATA. sg_format (SCSI FORMAT UNIT) fails on those with
    "Illegal request"; they want hdparm instead.

    `on_status` (v0.6.3+) is an optional callable that receives
    human-readable progress messages during the erase — "SAT passthrough
    starting", "SAT aborted, falling back to hdparm", etc. The
    orchestrator passes a callback that updates the drive card's
    sublabel so the operator sees which path is running live. Only the
    SATA path currently emits status (SAS sg_format and NVMe format are
    black-box subprocess calls with no intermediate progress signal).
    """
    effective = drive.transport
    if effective == Transport.SAS:
        from driveforge.core.drive import detect_true_transport

        refined = detect_true_transport(drive.device_path)
        if refined in (Transport.SATA, Transport.SAS, Transport.NVME):
            effective = refined

    if effective == Transport.SATA:
        _sata_secure_erase(
            drive.device_path,
            owner=drive.serial,
            capacity_bytes=drive.capacity_bytes,
            on_status=on_status,
        )
    elif effective == Transport.SAS:
        _sas_secure_erase(drive.device_path, owner=drive.serial, capacity_bytes=drive.capacity_bytes)
    elif effective == Transport.NVME:
        _nvme_format(drive.device_path, owner=drive.serial)
    else:
        raise EraseError(f"no erase path for transport={effective}")
