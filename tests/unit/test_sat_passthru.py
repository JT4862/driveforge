"""Unit tests for the v0.3.0 SAT passthrough module.

Three layers of testing:

  1. CDB construction — `_build_atapt16_cdb` produces the exact bytes
     SAT-3 §13.4 specifies for ATA SECURITY family commands. Wrong
     CDB bytes here = real drives reject the command, and we'd only
     find out on hardware. Spec-conformance unit tests are the cheap
     guard.

  2. Password-block construction — `_build_password_block` produces
     a 512-byte buffer with the password ASCII at bytes 2-33, NUL
     padded, and word 0 control bits correctly set. Per ATA8-ACS
     §7.45.

  3. Top-level dispatch — `sat_secure_erase` invokes the three ATA
     commands in the mandatory PASSWORD → PREPARE → ERASE UNIT order.
     `process.run` is monkeypatched so we capture the argv list +
     temp-file contents (where applicable) without actually running
     `sg_raw` against /dev/null on the dev host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from driveforge.core import sat_passthru
from driveforge.core.process import ProcessResult


# ---------------------------------------------------------------- CDB construction


def test_cdb_for_security_erase_unit_data_out() -> None:
    """SECURITY ERASE UNIT (0xF4) is a PIO Data-Out, single 512-byte
    block, LBA mode. Per SAT-3 §13.4 + ATA8-ACS §7.45 the CDB must be:

        85 0A 06 00 00 00 01 00 00 00 00 00 00 40 F4 00

    Byte 1 = 0x0A: protocol(5)<<1 | extend(0)
    Byte 2 = 0x06: t_dir=0, byt_blok=1, t_length=2
    Byte 6 = 0x01: count = 1 block
    Byte 13 = 0x40: device, LBA mode
    Byte 14 = 0xF4: ATA SECURITY ERASE UNIT
    """
    cdb = sat_passthru._build_atapt16_cdb(
        ata_cmd=sat_passthru.ATA_SECURITY_ERASE_UNIT,
        protocol=sat_passthru.ATA_PROTO_PIO_DATA_OUT,
        has_data=True,
    )
    expected = [
        0x85, 0x0A, 0x06, 0x00, 0x00,
        0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x40, 0xF4, 0x00,
    ]
    assert cdb == expected, f"got {[hex(b) for b in cdb]}"


def test_cdb_for_security_erase_prepare_non_data() -> None:
    """SECURITY ERASE PREPARE (0xF3) carries no data — protocol=3
    (Non-data), count=0, byte 2 = 0x00 (CK_COND=0).

    CK_COND must be 0 on non-data commands: with CK_COND=1 the SAT
    layer returns CHECK CONDITION even on success (sense key
    RECOVERED_ERROR carrying the ATA Status Return), which sg_raw
    surfaces as non-zero and our caller treats as failure. v0.4.3
    fix — see the docstring in `_build_atapt16_cdb` for detail."""
    cdb = sat_passthru._build_atapt16_cdb(
        ata_cmd=sat_passthru.ATA_SECURITY_ERASE_PREPARE,
        protocol=sat_passthru.ATA_PROTO_NON_DATA,
        has_data=False,
    )
    expected = [
        0x85, 0x06, 0x00, 0x00, 0x00,
        0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x40, 0xF3, 0x00,
    ]
    assert cdb == expected, f"got {[hex(b) for b in cdb]}"


def test_non_data_cdb_has_ck_cond_cleared() -> None:
    """Regression for v0.4.3. Non-data SAT passthrough commands MUST
    have CK_COND=0 (bit 5 of CDB byte 2). With CK_COND=1 the SAT layer
    returns CHECK CONDITION on success — sense key RECOVERED_ERROR
    carrying the ATA Status Return descriptor — which sg_raw reports
    as non-zero exit, and our caller treats as failure. Observed in
    the wild on WDC WD1000CHTZ behind an LSI 9207-8i as spurious
    'SECURITY ERASE PREPARE failed' with status=0x50 (DRDY+DSC set,
    ERR clear — i.e. the command actually succeeded)."""
    cdb = sat_passthru._build_atapt16_cdb(
        ata_cmd=sat_passthru.ATA_SECURITY_ERASE_PREPARE,
        protocol=sat_passthru.ATA_PROTO_NON_DATA,
        has_data=False,
    )
    # Byte 2 bit 5 = CK_COND. Must be 0.
    assert (cdb[2] & 0x20) == 0, (
        f"CK_COND bit set on non-data CDB (byte 2 = {cdb[2]:#04x}) — "
        "SAT will return CHECK CONDITION on success and sg_raw will "
        "report the command as failed"
    )


def test_cdb_for_security_set_password_data_out() -> None:
    """SECURITY SET PASSWORD (0xF1) — same shape as ERASE UNIT, only
    the ATA command byte differs."""
    cdb = sat_passthru._build_atapt16_cdb(
        ata_cmd=sat_passthru.ATA_SECURITY_SET_PASSWORD,
        protocol=sat_passthru.ATA_PROTO_PIO_DATA_OUT,
        has_data=True,
    )
    assert cdb[14] == 0xF1, "byte 14 must be the ATA SET PASSWORD opcode"
    assert cdb[1] == 0x0A, "protocol=PIO Data-Out → byte 1 = 0x0A"
    assert cdb[2] == 0x06, "byt_blok=1 t_length=2 t_dir=to_dev"


def test_cdb_for_unlock_and_disable() -> None:
    """SECURITY UNLOCK (0xF2) and SECURITY DISABLE PASSWORD (0xF6)
    have the same CDB shape as SET PASSWORD — only the opcode byte
    differs. They're the recovery-path commands."""
    unlock = sat_passthru._build_atapt16_cdb(
        ata_cmd=sat_passthru.ATA_SECURITY_UNLOCK,
        protocol=sat_passthru.ATA_PROTO_PIO_DATA_OUT,
        has_data=True,
    )
    disable = sat_passthru._build_atapt16_cdb(
        ata_cmd=sat_passthru.ATA_SECURITY_DISABLE_PASSWORD,
        protocol=sat_passthru.ATA_PROTO_PIO_DATA_OUT,
        has_data=True,
    )
    assert unlock[14] == 0xF2
    assert disable[14] == 0xF6
    # Apart from opcode + control, every other byte must match between
    # the two — same transfer parameters.
    for i, (u, d) in enumerate(zip(unlock, disable)):
        if i == 14:
            continue
        assert u == d, f"CDB byte {i} differs unexpectedly: unlock={u:#x} disable={d:#x}"


# ---------------------------------------------------------------- password block


def test_password_block_layout() -> None:
    """ATA8-ACS §7.45 specifies word 0 = control, words 1-16 = password
    (32 bytes), rest must be zero. Block is exactly 512 bytes."""
    block = sat_passthru._build_password_block("driveforge")
    assert len(block) == 512
    # Word 0 = 0x0000 for normal user-mode erase
    assert block[0:2] == b"\x00\x00"
    # Password starts at byte 2, ASCII, NUL-padded to 32 bytes
    assert block[2:12] == b"driveforge"
    assert block[12:34] == b"\x00" * 22
    # Reserved tail must be zero
    assert block[34:] == b"\x00" * (512 - 34)


def test_password_block_truncates_long_password() -> None:
    """Passwords longer than 32 bytes are truncated, not rejected.
    This is per-spec — the drive only consumes the first 32 bytes."""
    long_pw = "a" * 64
    block = sat_passthru._build_password_block(long_pw)
    assert block[2:34] == b"a" * 32
    assert block[34:36] == b"\x00\x00"


def test_password_block_enhanced_erase_bit() -> None:
    """When erase_enhanced=True, word 0 bit 1 must be set (= 0x0002).
    Default normal erase leaves it clear."""
    normal = sat_passthru._build_password_block("x", erase_enhanced=False)
    enhanced = sat_passthru._build_password_block("x", erase_enhanced=True)
    assert normal[0:2] == b"\x00\x00"
    assert enhanced[0:2] == b"\x02\x00"


# ---------------------------------------------------------------- top-level dispatch


class _RunSpy:
    """Records every `process.run` invocation made during a SAT call,
    plus reads back the temp-file payload before sat_passthru's
    finally block deletes it. Lets tests assert on argv ordering AND
    on exact bytes sent without touching real /dev/sd*."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.next_result = ProcessResult(argv=[], returncode=0, stdout="", stderr="")

    def __call__(self, argv, *, check=False, timeout=None, owner=None):
        captured = {
            "argv": list(argv),
            "owner": owner,
            "timeout": timeout,
            "payload": None,
        }
        # If this call has `-i <file>`, read the payload before the
        # caller's finally unlinks it.
        if "-i" in argv:
            i_idx = argv.index("-i")
            payload_path = Path(argv[i_idx + 1])
            if payload_path.exists():
                captured["payload"] = payload_path.read_bytes()
        self.calls.append(captured)
        return ProcessResult(argv=list(argv), returncode=0, stdout="", stderr="")


def test_sat_secure_erase_invokes_three_commands_in_order(monkeypatch) -> None:
    """The full SECURITY SET PASSWORD → PREPARE → ERASE UNIT sequence
    must run on each call to sat_secure_erase. Order matters — ATA
    spec rejects ERASE UNIT unless PREPARE was the most-recent
    security command."""
    spy = _RunSpy()
    monkeypatch.setattr(sat_passthru, "run", spy)
    sat_passthru.sat_secure_erase("/dev/sdfake", password="testpw", timeout_s=300)
    assert len(spy.calls) == 3, "must invoke three ATA security commands"
    # Verify the ATA opcode (CDB byte 14) for each call.
    opcodes = [int(call["argv"][-2], 16) for call in spy.calls]
    assert opcodes == [
        sat_passthru.ATA_SECURITY_SET_PASSWORD,
        sat_passthru.ATA_SECURITY_ERASE_PREPARE,
        sat_passthru.ATA_SECURITY_ERASE_UNIT,
    ], f"command order wrong: {[hex(c) for c in opcodes]}"


def test_sat_secure_erase_long_timeout_only_on_erase_unit(monkeypatch) -> None:
    """SECURITY ERASE UNIT can take hours; the SG_IO timeout must
    pass through to sg_raw's `-t` flag for that call. SET PASSWORD
    and PREPARE are sub-second commands."""
    spy = _RunSpy()
    monkeypatch.setattr(sat_passthru, "run", spy)
    sat_passthru.sat_secure_erase("/dev/sdfake", timeout_s=3600)
    # sg_raw arg layout: [..., "-t", "<seconds>", device, ...cdb]
    timeouts = [int(call["argv"][call["argv"].index("-t") + 1]) for call in spy.calls]
    assert timeouts[0] == 60, "SET PASSWORD: short timeout"
    assert timeouts[1] == 60, "ERASE PREPARE: short timeout"
    assert timeouts[2] == 3600, "ERASE UNIT: caller-supplied long timeout"


def test_sat_secure_erase_payload_is_512_bytes_with_password(monkeypatch) -> None:
    """The data-out payload sent for SET PASSWORD and ERASE UNIT must
    be the 512-byte security block with the password at bytes 2-33."""
    spy = _RunSpy()
    monkeypatch.setattr(sat_passthru, "run", spy)
    sat_passthru.sat_secure_erase("/dev/sdfake", password="hello123", timeout_s=60)

    # SET PASSWORD (call 0) carries payload
    assert spy.calls[0]["payload"] is not None
    assert len(spy.calls[0]["payload"]) == 512
    assert spy.calls[0]["payload"][2:10] == b"hello123"

    # ERASE PREPARE (call 1) is non-data — no -i flag
    assert "-i" not in spy.calls[1]["argv"]
    assert spy.calls[1]["payload"] is None

    # ERASE UNIT (call 2) carries payload
    assert spy.calls[2]["payload"] is not None
    assert len(spy.calls[2]["payload"]) == 512
    assert spy.calls[2]["payload"][2:10] == b"hello123"


def test_sat_secure_erase_propagates_owner(monkeypatch) -> None:
    """Every sg_raw call must register under the drive's serial as
    the subprocess owner so abort_drive can kill them as a group."""
    spy = _RunSpy()
    monkeypatch.setattr(sat_passthru, "run", spy)
    sat_passthru.sat_secure_erase("/dev/sdfake", owner="SN-XYZ-1", timeout_s=60)
    for call in spy.calls:
        assert call["owner"] == "SN-XYZ-1"


def test_sat_failure_raises_sat_passthru_error(monkeypatch) -> None:
    """sg_raw non-zero must surface as SatPassthruError with a useful
    message — the orchestrator's secure-erase failure path needs the
    detail to render a meaningful failed-card."""
    def fake_run(argv, *, check=False, timeout=None, owner=None):
        return ProcessResult(
            argv=list(argv),
            returncode=2,
            stdout="",
            stderr="sg_raw: SCSI status: Check Condition\nfixed sense: ABORTED COMMAND",
        )
    monkeypatch.setattr(sat_passthru, "run", fake_run)
    with pytest.raises(sat_passthru.SatPassthruError) as exc_info:
        sat_passthru.security_set_password("/dev/sdfake")
    msg = str(exc_info.value)
    assert "/dev/sdfake" in msg
    assert "SECURITY SET PASSWORD" in msg
    assert "ABORTED COMMAND" in msg


def test_sat_secure_erase_cleans_up_password_on_mid_sequence_failure(monkeypatch) -> None:
    """v0.4.4 regression. If SET PASSWORD succeeds but PREPARE or
    ERASE UNIT fails, the erase function MUST attempt to DISABLE
    PASSWORD on the way out — otherwise the drive stays
    security-enabled with a lingering password that breaks future
    attempts. The recovery workflow only covers pulled-drive cases,
    not failed-in-place — so cleanup is the erase function's job."""
    call_log: list[int] = []

    def fake_run(argv, *, check=False, timeout=None, owner=None):
        # Extract the ATA opcode (CDB byte 14) to tell which command this is
        opcode = int(argv[-2], 16)
        call_log.append(opcode)
        # SET PASSWORD succeeds; PREPARE fails; we then expect DISABLE to run
        if opcode == sat_passthru.ATA_SECURITY_ERASE_PREPARE:
            return ProcessResult(
                argv=list(argv),
                returncode=2,
                stdout="",
                stderr="sg_raw: SCSI status: Check Condition",
            )
        return ProcessResult(argv=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sat_passthru, "run", fake_run)

    with pytest.raises(sat_passthru.SatPassthruError) as exc_info:
        sat_passthru.sat_secure_erase("/dev/sdfake", timeout_s=60)

    # Verify order: SET PASSWORD → PREPARE (failed) → DISABLE PASSWORD (cleanup)
    # No ERASE UNIT should fire since PREPARE failed first.
    assert call_log == [
        sat_passthru.ATA_SECURITY_SET_PASSWORD,
        sat_passthru.ATA_SECURITY_ERASE_PREPARE,
        sat_passthru.ATA_SECURITY_DISABLE_PASSWORD,
    ], f"command order wrong: {[hex(c) for c in call_log]}"

    # Original error from PREPARE is what propagates, NOT the cleanup result.
    assert "ERASE PREPARE" in str(exc_info.value)


def test_sat_secure_erase_does_not_run_cleanup_on_success(monkeypatch) -> None:
    """Guardrail: on a fully-successful erase (SET PASSWORD → PREPARE →
    ERASE UNIT all succeed), we must NOT issue DISABLE PASSWORD. The
    ERASE UNIT itself clears the password as part of its operation;
    adding an extra DISABLE would be wasted round-trip and could error
    on some drive firmware."""
    call_log: list[int] = []

    def fake_run(argv, *, check=False, timeout=None, owner=None):
        opcode = int(argv[-2], 16)
        call_log.append(opcode)
        return ProcessResult(argv=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sat_passthru, "run", fake_run)
    sat_passthru.sat_secure_erase("/dev/sdfake", timeout_s=60)

    assert sat_passthru.ATA_SECURITY_DISABLE_PASSWORD not in call_log, (
        "DISABLE PASSWORD must not fire on successful erase — "
        f"calls were: {[hex(c) for c in call_log]}"
    )


def test_sat_secure_erase_cleanup_failure_does_not_mask_original_error(monkeypatch) -> None:
    """If both the main command AND the cleanup DISABLE PASSWORD fail,
    the caller should see the ORIGINAL error (which is what the
    operator needs to diagnose), not the cleanup error (secondary)."""
    def fake_run(argv, *, check=False, timeout=None, owner=None):
        opcode = int(argv[-2], 16)
        if opcode == sat_passthru.ATA_SECURITY_SET_PASSWORD:
            return ProcessResult(argv=list(argv), returncode=0, stdout="", stderr="")
        # PREPARE fails with distinctive marker
        if opcode == sat_passthru.ATA_SECURITY_ERASE_PREPARE:
            return ProcessResult(
                argv=list(argv), returncode=2, stdout="", stderr="ORIGINAL_ERROR_MARKER"
            )
        # DISABLE also fails, with different marker
        if opcode == sat_passthru.ATA_SECURITY_DISABLE_PASSWORD:
            return ProcessResult(
                argv=list(argv), returncode=2, stdout="", stderr="CLEANUP_ERROR_MARKER"
            )
        return ProcessResult(argv=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sat_passthru, "run", fake_run)

    with pytest.raises(sat_passthru.SatPassthruError) as exc_info:
        sat_passthru.sat_secure_erase("/dev/sdfake", timeout_s=60)

    msg = str(exc_info.value)
    assert "ORIGINAL_ERROR_MARKER" in msg, "original failure must propagate"
    assert "CLEANUP_ERROR_MARKER" not in msg, "cleanup error must not mask original"


def test_sat_secure_erase_temp_file_cleaned_up(monkeypatch, tmp_path) -> None:
    """The password-bearing temp file MUST be unlinked after the
    sg_raw call returns, even on failure. Leaving secrets in /tmp
    defeats the throwaway-credential design. Capture the temp paths,
    let the SAT call run, then verify all are gone."""
    seen_paths: list[Path] = []

    def fake_run(argv, *, check=False, timeout=None, owner=None):
        if "-i" in argv:
            i_idx = argv.index("-i")
            seen_paths.append(Path(argv[i_idx + 1]))
        return ProcessResult(argv=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sat_passthru, "run", fake_run)
    sat_passthru.sat_secure_erase("/dev/sdfake", timeout_s=60)
    # SET PASSWORD + ERASE UNIT each had a payload tempfile
    assert len(seen_paths) == 2
    for p in seen_paths:
        assert not p.exists(), f"password tempfile {p} was not cleaned up"
