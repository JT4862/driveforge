"""ATA error decoder for user-facing failure banners (v0.6.3+).

When a SAT passthrough command fails, sg_raw surfaces a SCSI Check
Condition with an embedded ATA Status Return descriptor. The raw
text looks like:

    SCSI Status: Check Condition
    Sense Information:
    Descriptor format, current; Sense key: Aborted Command
    Additional sense: No additional sense information
      Descriptor type: ATA Status Return: extend=0 error=0x4
            count=0x1 lba=0x000000 device=0x40 status=0x51

That's accurate but unreadable for operators. This module parses it
into a plain-English cause + suggested next step, which the
dashboard failure banner renders in place of the raw dump.

We keep the mapping table intentionally small: the common ATA error
patterns during secure-erase, plus a catchall. Anything not in the
table falls through to the raw sg_raw output so we don't hide real
information behind a too-generic "command failed" message.

Design: pure function. No I/O, no subprocess calls, no state. Takes
the sg_raw error string, returns a DecodedError. Easy to unit-test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DecodedError:
    """The human-readable form of a raw sg_raw / SAT passthrough error.

    `cause` is the "what happened" — one-sentence plain English.
    `next_step` is the "what can the operator do" — may be a
    suggestion DriveForge will auto-try (e.g. "falling back to
    hdparm"), an instruction the operator must carry out, or a
    verdict like "drive likely failing; replace."
    `severity` categorizes for UI styling:
      - `info`    — working-as-designed, no action needed
      - `warn`    — DriveForge auto-handling it, operator should be
                    aware
      - `error`   — hard failure, drive graded F, operator action
                    required
    """

    cause: str
    next_step: str
    severity: str  # "info" | "warn" | "error"


# Regex patterns that pull the useful fields out of sg_raw's default
# error dump. Numbers are hex per sg3_utils default.
_RE_SENSE_KEY = re.compile(r"Sense key:\s+([A-Za-z ]+)", re.IGNORECASE)
_RE_ATA_ERROR = re.compile(r"error=0x([0-9a-f]+)", re.IGNORECASE)
_RE_ATA_STATUS = re.compile(r"status=0x([0-9a-f]+)", re.IGNORECASE)


def _parse_fields(raw: str) -> tuple[str | None, int | None, int | None]:
    """Extract (sense_key, ata_error_register, ata_status_register)
    from a raw sg_raw error dump. Any field missing → None."""
    sk_m = _RE_SENSE_KEY.search(raw)
    sk = sk_m.group(1).strip().lower() if sk_m else None

    ae_m = _RE_ATA_ERROR.search(raw)
    ae = int(ae_m.group(1), 16) if ae_m else None

    as_m = _RE_ATA_STATUS.search(raw)
    ast = int(as_m.group(1), 16) if as_m else None

    return (sk, ae, ast)


def decode_secure_erase_error(raw: str) -> DecodedError:
    """Decode a SAT passthrough SECURITY ERASE failure into operator-
    facing text. Returns a DecodedError with a best-guess cause +
    suggestion. Falls back to a generic wrapper when the error pattern
    doesn't match anything in the table — better to say "we don't
    know exactly what went wrong" than to make up a confident wrong
    guess.

    Called from the failure path in `_sata_secure_erase` (via
    `erase.py`) — only after the auto-fallback to hdparm has ALSO
    failed (otherwise the fallback succeeds and there's nothing to
    decode). This is the "both paths refused" → operator-guidance
    layer.
    """
    sense_key, ata_error, ata_status = _parse_fields(raw or "")

    # ATA error register bits per ATA8-ACS §7.11:
    #   bit 2 = ABRT (Command aborted)
    #   bit 6 = UNC  (Uncorrectable error)
    #   bit 7 = BBK  (Bad block detected)
    ABRT = 0x04
    UNC = 0x40
    BBK = 0x80

    # ATA status register bits:
    #   bit 0 = ERR  (Error)
    #   bit 3 = DRQ  (Data request)
    #   bit 5 = DF   (Device fault)
    #   bit 6 = DRDY (Device ready)
    #   bit 7 = BSY  (Busy)
    DF = 0x20

    # ------------------------------------------------------ ABRT on ERASE
    # Sense = aborted-command, ATA error = ABRT. Most common pattern.
    # Drive acknowledged the command but refused it. Could be:
    #   - Linux libata auto-freeze issued during udev probe on reinsert
    #     (drive aborts all security commands; `hdparm -I` may still
    #     report "not frozen" because the freeze fires during probe,
    #     not at identify-read time. This is the OVERWHELMING majority
    #     of ABRT-on-ERASE-UNIT cases on server hosts with many drives.)
    #   - BIOS issued SECURITY FREEZE LOCK during POST
    #   - Drive firmware refuses SECURITY ERASE UNIT over SAT specifically
    #     (workaround: hdparm native path — already attempted before the
    #     decoder runs, so if we got here, that also failed)
    #   - Drive's 5-attempt security count expired
    #   - Drive firmware has ATA security feature disabled in its
    #     current configuration
    if sense_key == "aborted command" and ata_error is not None and (ata_error & ABRT):
        return DecodedError(
            cause=(
                "Drive refused SECURITY ERASE UNIT over both SAT passthrough "
                "AND native hdparm ATA paths with ABRT (command aborted "
                "by drive firmware). Most common root cause: Linux's libata "
                "driver auto-issued SECURITY FREEZE LOCK during the post-"
                "reinsert udev probe. hdparm -I may still report 'not "
                "frozen' because the freeze fires during probe, not at "
                "identify-read time."
            ),
            next_step=(
                "In priority order: "
                "(1) Suspend + resume the host (systemctl suspend) — "
                "libata's freeze does NOT persist across suspend; drive "
                "accepts security commands after resume. "
                "(2) Move the drive to a USB-SATA enclosure on a host "
                "whose kernel doesn't auto-freeze. "
                "(3) Reboot with BIOS 'security freeze lock' disabled, "
                "if your firmware exposes that option. "
                "(4) For self-encrypting drives (SEDs), a PSID-based "
                "factory reset from the physical label's PSID works "
                "independently of the security freeze (not yet automated "
                "in DriveForge). "
                "(5) If the drive is on the known-flaky advisory list, "
                "set aside as known-bad and move on."
            ),
            severity="error",
        )

    # ------------------------------------------------------ Device fault
    # DF bit set in status = internal drive fault. Hard failure indicator.
    if ata_status is not None and (ata_status & DF):
        return DecodedError(
            cause=(
                "Drive reported an internal device fault during SECURITY "
                "ERASE — drive hardware is likely failing."
            ),
            next_step=(
                "Do not attempt further testing on this drive. Replace / "
                "set aside as failed hardware."
            ),
            severity="error",
        )

    # ------------------------------------------------------ UNC / BBK
    # Uncorrectable read / bad block during erase. Drive tried but
    # couldn't complete due to media damage.
    if ata_error is not None and (ata_error & (UNC | BBK)):
        return DecodedError(
            cause=(
                "Drive encountered uncorrectable media errors during "
                "SECURITY ERASE — drive is physically failing."
            ),
            next_step=(
                "Drive is not sanitize-safe (may still hold recoverable "
                "data even after reported erase). Physical destruction "
                "or PSID factory reset (for SEDs) is the safe disposition."
            ),
            severity="error",
        )

    # ------------------------------------------------------ Timeout / no response
    # No ATA error register at all typically means sg_raw never got a
    # response from the drive — either HBA-level timeout or drive is
    # genuinely unresponsive.
    if ata_error is None and (
        "timed out" in (raw or "").lower() or "timeout" in (raw or "").lower()
    ):
        return DecodedError(
            cause=(
                "Drive did not respond to SECURITY ERASE within the "
                "timeout window."
            ),
            next_step=(
                "Drive may be genuinely failing, or the HBA may have lost "
                "the link. Check `dmesg` for SAS/SATA bus errors and "
                "consider reseating or replacing the drive."
            ),
            severity="error",
        )

    # ------------------------------------------------------ Fallback
    # Unknown pattern — pass through the raw text so the operator at
    # least has what we had. Don't pretend we know what happened.
    return DecodedError(
        cause="Secure erase failed with an unrecognized error pattern.",
        next_step=(
            f"Raw error from drive: {raw.strip()[:400]}. "
            "If this pattern recurs, report it so the error decoder can "
            "learn it."
        ),
        severity="error",
    )
