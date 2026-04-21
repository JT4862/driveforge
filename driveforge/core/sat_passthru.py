"""SAT (SCSI/ATA Translation) passthrough for ATA security commands.

v0.3.0. Replaces `hdparm --security-erase` for SATA drives. The legacy
hdparm path uses Linux's `HDIO_DRIVE_TASKFILE` ioctl, which modern
Debian kernels (12+) no longer provide — drives behind a SAS HBA fail
with `CONFIG_IDE_TASK_IOCTL`. This module wraps the same ATA security
commands inside SCSI ATA-PASS-THROUGH(16) CDBs and submits them via
`sg_raw`, which the HBA's SAT layer unwraps and forwards to the drive
as the original ATA command.

Why this works on hardware where hdparm doesn't:

  - SAT-3 (`INCITS T10/2105-D`) requires every SAS-family HBA that
    carries SATA drives to support the ATA-PASS-THROUGH(16) opcode
    (`0x85`). That mandate has been in place since 2008.
  - smartctl already uses ATA-PASS-THROUGH(16) for SMART reads on
    SATA-on-SAS — we know smartctl works on the NX-3200, which proves
    the HBA-side machinery is functional. We're just using the same
    code path for security commands instead of identify/log reads.
  - sg_raw issues SG_IO directly, bypassing the dead HDIO ioctl
    entirely.

Commands implemented (all `0xFx` family from ATA8-ACS):

  - 0xF1 SECURITY SET PASSWORD       (PIO Data-Out, 512-byte payload)
  - 0xF2 SECURITY UNLOCK             (PIO Data-Out, 512-byte payload)
  - 0xF3 SECURITY ERASE PREPARE      (Non-data)
  - 0xF4 SECURITY ERASE UNIT         (PIO Data-Out, 512-byte payload — long timeout)
  - 0xF6 SECURITY DISABLE PASSWORD   (PIO Data-Out, 512-byte payload)

The 512-byte security data block layout per ATA8-ACS section 7.45:

  Word 0,  bit 0:    Identifier (0=user, 1=master)
  Word 0,  bit 1:    Erase mode (0=normal, 1=enhanced)  [SECURITY ERASE UNIT only]
  Words 1-16:        32 bytes of password (ASCII, NUL-padded)
  Words 17-255:      Reserved (zeros)

We always use a fixed user password (`driveforge`) because the password
is set + erased + cleared in the same pipeline run — it never persists
past the secure-erase phase. If a drive is pulled mid-pipeline, the
recovery path knows the password and can unlock + disable it on
re-insert.
"""

from __future__ import annotations

import logging
import os
import tempfile

from driveforge.core.process import ProcessResult, run

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- ATA opcodes

ATA_SECURITY_SET_PASSWORD = 0xF1
ATA_SECURITY_UNLOCK = 0xF2
ATA_SECURITY_ERASE_PREPARE = 0xF3
ATA_SECURITY_ERASE_UNIT = 0xF4
ATA_SECURITY_DISABLE_PASSWORD = 0xF6

# SCSI ATA-PASS-THROUGH (16) opcode — see SAT-3 §7.6.
ATA_PT_16 = 0x85

# ATA-PASS-THROUGH protocol field values (CDB byte 1, bits 4:1).
# Per SAT-3 Table 84.
ATA_PROTO_NON_DATA = 3        # commands with no data transfer
ATA_PROTO_PIO_DATA_OUT = 5    # commands that send data to the drive

# Default fixed password. Lifecycle is single-pipeline — set just
# before SECURITY ERASE UNIT, cleared by the erase itself, with the
# recovery path also knowing this string for unlock/disable on a
# pulled-mid-erase drive.
DEFAULT_PASSWORD = "driveforge"


class SatPassthruError(RuntimeError):
    """Raised when a SAT passthrough command fails (sg_raw non-zero, or
    the underlying ATA command returned a sense error). The message
    carries enough detail to surface in the dashboard's failure card."""


# ---------------------------------------------------------------- helpers


def _build_atapt16_cdb(
    *,
    ata_cmd: int,
    protocol: int,
    has_data: bool,
) -> list[int]:
    """Construct a 16-byte ATA-PASS-THROUGH CDB for an ATA security
    command. All security commands target LBA 0 with 1 block of
    512-byte data (or no data for SECURITY ERASE PREPARE).

    See SAT-3 §13.4 for the byte-by-byte field definitions; this is
    the standard encoding for an LBA-mode command with a single-block
    PIO data transfer or a non-data command.
    """
    # Byte 1: [bits 4:1] = protocol, bit 0 = extend (always 0 for legacy security cmds)
    byte1 = (protocol & 0x0F) << 1
    if has_data:
        # PIO Data-Out, single 512-byte block:
        #   bit 7   = off_line       = 0
        #   bit 6   = ck_cond        = 0  (no automatic sense return)
        #   bit 5   = t_type         = 0  (LBA addressing)
        #   bit 4   = t_dir          = 0  (to device for OUT)
        #   bit 3   = byt_blok       = 1  (count is in blocks)
        #   bits 2:0 = t_length      = 2  (count comes from count register)
        byte2 = 0x06
        count_lsb = 0x01  # 1 × 512-byte block
    else:
        # Non-data: protocol field already says "no transfer". Set ck_cond
        # so the HBA returns sense data on completion — lets us tell a
        # successful SECURITY ERASE PREPARE from a silent failure.
        byte2 = 0x20
        count_lsb = 0x00
    return [
        ATA_PT_16,
        byte1,
        byte2,
        0x00,           # features 15:8 (always 0 for security cmds)
        0x00,           # features 7:0
        0x00,           # count 15:8
        count_lsb,      # count 7:0
        0x00,           # LBA 31:24 (low LBA, 28-bit half)
        0x00,           # LBA 7:0
        0x00,           # LBA 15:8
        0x00,           # LBA 23:16
        0x00,           # LBA 39:32 (high LBA, unused for security cmds)
        0x00,           # LBA 47:40
        0x40,           # device — bit 6 = LBA mode (always set for modern drives)
        ata_cmd & 0xFF, # ATA command opcode
        0x00,           # control byte
    ]


def _build_password_block(password: str, *, erase_enhanced: bool = False) -> bytes:
    """Build the 512-byte security data block per ATA8-ACS §7.45.

    Word 0 control bits:
      bit 0 = Identifier (0 = User password, 1 = Master). We always use
              user-mode for the throwaway pipeline password.
      bit 1 = Erase mode (0 = Normal, 1 = Enhanced). Normal is fine —
              modern drives implement Normal as a crypto erase on SED
              models and a full overwrite on plain HDDs; either way the
              data is gone.

    Password occupies bytes 2-33 (16 ATA "words"), ASCII, NUL-padded
    to 32 bytes. Anything beyond byte 33 must be zero per spec.
    """
    word0 = 0x0000
    if erase_enhanced:
        word0 |= 0x0002
    block = bytearray(512)
    block[0:2] = word0.to_bytes(2, "little")
    pw_bytes = password.encode("ascii", errors="ignore")[:32]
    block[2 : 2 + len(pw_bytes)] = pw_bytes
    return bytes(block)


def _run_sg_raw_data_out(
    device: str,
    cdb: list[int],
    payload: bytes,
    *,
    timeout_s: int,
    owner: str | None = None,
) -> ProcessResult:
    """Execute a single ATA-PASS-THROUGH PIO Data-Out command via
    sg_raw, with the payload supplied as a temp file (sg_raw's `-i`
    flag reads the data-out buffer from disk).

    The temp file is unlinked before return regardless of outcome —
    leaving an erase password in /tmp would defeat the whole point of
    the throwaway-credential model.
    """
    tf = tempfile.NamedTemporaryFile(delete=False, prefix="driveforge-sat-")
    try:
        tf.write(payload)
        tf.flush()
        tf.close()
        cdb_hex = [f"{b:02x}" for b in cdb]
        argv = [
            "sg_raw",
            "-s", str(len(payload)),
            "-i", tf.name,
            "-t", str(timeout_s),
            device,
            *cdb_hex,
        ]
        # Outer subprocess timeout adds 30 s headroom over the SG_IO
        # timeout so we don't kill sg_raw mid-result-collection on a
        # genuinely-completed-but-slow erase.
        return run(argv, owner=owner, timeout=timeout_s + 30)
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass


def _run_sg_raw_non_data(
    device: str,
    cdb: list[int],
    *,
    timeout_s: int,
    owner: str | None = None,
) -> ProcessResult:
    """Execute a single ATA-PASS-THROUGH non-data command via sg_raw."""
    cdb_hex = [f"{b:02x}" for b in cdb]
    argv = ["sg_raw", "-t", str(timeout_s), device, *cdb_hex]
    return run(argv, owner=owner, timeout=timeout_s + 30)


# ---------------------------------------------------------------- public API


def security_set_password(
    device: str,
    *,
    password: str = DEFAULT_PASSWORD,
    owner: str | None = None,
) -> None:
    """Set the user password on a SATA drive via SAT passthrough.
    Required setup step before SECURITY ERASE UNIT can be issued."""
    cdb = _build_atapt16_cdb(
        ata_cmd=ATA_SECURITY_SET_PASSWORD,
        protocol=ATA_PROTO_PIO_DATA_OUT,
        has_data=True,
    )
    payload = _build_password_block(password)
    r = _run_sg_raw_data_out(device, cdb, payload, timeout_s=60, owner=owner)
    if not r.ok:
        raise SatPassthruError(
            f"SECURITY SET PASSWORD failed on {device}: "
            f"{(r.stderr or r.stdout or '').strip() or 'sg_raw non-zero'}"
        )


def security_erase_prepare(
    device: str,
    *,
    owner: str | None = None,
) -> None:
    """Issue SECURITY ERASE PREPARE. ATA spec mandates this be the
    immediately-preceding command before SECURITY ERASE UNIT — the
    drive ignores ERASE UNIT otherwise (anti-replay-attack guard)."""
    cdb = _build_atapt16_cdb(
        ata_cmd=ATA_SECURITY_ERASE_PREPARE,
        protocol=ATA_PROTO_NON_DATA,
        has_data=False,
    )
    r = _run_sg_raw_non_data(device, cdb, timeout_s=60, owner=owner)
    if not r.ok:
        raise SatPassthruError(
            f"SECURITY ERASE PREPARE failed on {device}: "
            f"{(r.stderr or r.stdout or '').strip() or 'sg_raw non-zero'}"
        )


def security_erase_unit(
    device: str,
    *,
    password: str = DEFAULT_PASSWORD,
    timeout_s: int = 12 * 3600,
    owner: str | None = None,
) -> None:
    """Issue SECURITY ERASE UNIT — the actual destructive command.

    Drive-side runtime is hours for plain HDDs (full media overwrite)
    and seconds-to-minutes for SSDs / SED HDDs (crypto erase). Caller
    supplies a `timeout_s` derived from the drive's own announced
    estimate (hdparm -I parsed estimate × 1.5) or a capacity-based
    fallback.

    The drive enters a not-ready state for the entire duration; sg_raw
    blocks until the drive reports completion via final-task-file
    status. Aborting mid-command via SIGTERM is unsafe — the drive may
    leave the security state in an indeterminate condition that needs
    a power cycle + manual unlock. The orchestrator's per-drive Abort
    button is disabled during secure_erase for exactly this reason.
    """
    cdb = _build_atapt16_cdb(
        ata_cmd=ATA_SECURITY_ERASE_UNIT,
        protocol=ATA_PROTO_PIO_DATA_OUT,
        has_data=True,
    )
    payload = _build_password_block(password)
    r = _run_sg_raw_data_out(device, cdb, payload, timeout_s=timeout_s, owner=owner)
    if not r.ok:
        raise SatPassthruError(
            f"SECURITY ERASE UNIT failed on {device}: "
            f"{(r.stderr or r.stdout or '').strip() or 'sg_raw non-zero'}"
        )


def security_unlock(
    device: str,
    *,
    password: str = DEFAULT_PASSWORD,
    owner: str | None = None,
) -> None:
    """Recovery: unlock a drive that's still in security-locked state
    after a mid-erase pull. Same password the SET PASSWORD step used."""
    cdb = _build_atapt16_cdb(
        ata_cmd=ATA_SECURITY_UNLOCK,
        protocol=ATA_PROTO_PIO_DATA_OUT,
        has_data=True,
    )
    payload = _build_password_block(password)
    r = _run_sg_raw_data_out(device, cdb, payload, timeout_s=60, owner=owner)
    if not r.ok:
        raise SatPassthruError(
            f"SECURITY UNLOCK failed on {device}: "
            f"{(r.stderr or r.stdout or '').strip() or 'sg_raw non-zero'}"
        )


def security_disable_password(
    device: str,
    *,
    password: str = DEFAULT_PASSWORD,
    owner: str | None = None,
) -> None:
    """Recovery: clear the password so the drive returns to its
    factory security state (no password, not locked). Run after a
    successful SECURITY UNLOCK; the next pipeline can then SET its
    own password fresh."""
    cdb = _build_atapt16_cdb(
        ata_cmd=ATA_SECURITY_DISABLE_PASSWORD,
        protocol=ATA_PROTO_PIO_DATA_OUT,
        has_data=True,
    )
    payload = _build_password_block(password)
    r = _run_sg_raw_data_out(device, cdb, payload, timeout_s=60, owner=owner)
    if not r.ok:
        raise SatPassthruError(
            f"SECURITY DISABLE PASSWORD failed on {device}: "
            f"{(r.stderr or r.stdout or '').strip() or 'sg_raw non-zero'}"
        )


# ---------------------------------------------------------------- top-level


def sat_secure_erase(
    device: str,
    *,
    password: str = DEFAULT_PASSWORD,
    timeout_s: int = 12 * 3600,
    owner: str | None = None,
) -> None:
    """The full three-command secure-erase sequence via SAT passthrough.

    Sequence is non-negotiable per ATA8-ACS:
      1. SECURITY SET PASSWORD — drive enters security-enabled state
      2. SECURITY ERASE PREPARE — clears the anti-replay guard
      3. SECURITY ERASE UNIT — drive does the actual erase (hours)

    Steps 2 and 3 must execute back-to-back on the same drive — any
    intervening command resets the prepare state and ERASE UNIT will
    refuse. Caller MUST NOT introduce parallelism between PREPARE and
    UNIT for the same device.
    """
    security_set_password(device, password=password, owner=owner)
    security_erase_prepare(device, owner=owner)
    security_erase_unit(device, password=password, timeout_s=timeout_s, owner=owner)
