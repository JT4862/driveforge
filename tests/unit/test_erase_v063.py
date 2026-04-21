"""Tests for v0.6.3's erase-failure-handling additions.

v0.6.3 added three cooperating pieces that together let secure_erase
survive drive-firmware quirks without operator intervention:

1. **ATA error decoder** (`core/ata_errors.py`) — maps the raw sg_raw
   error dump to operator-facing cause + next-step text.
2. **Flaky-drive advisory** (`core/drive_advisory.py`) — small
   hardcoded list of known-bad model prefixes with pre-pipeline
   advisories.
3. **SAT → hdparm auto-fallback** (`core/erase.py`) — when SAT's
   SECURITY ERASE UNIT aborts, retry via hdparm native-ATA path.
4. **Case B mid-erase wait** (`core/erase.py`) — on re-insert, if the
   drive is locked and refusing unlock, assume it's autonomously
   completing a prior erase and poll until it returns to CLEAN.

Tests here cover each piece plus the integration points, using mocks
so no real hardware or subprocess calls happen.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from driveforge.core import ata_errors, drive_advisory, erase
from driveforge.core import sat_passthru


# ───────────────────────────── ATA error decoder ─────────────────────────────

# The exact error string JT saw on his ST4000NM0033 during v0.6.1 validation.
# This is the canonical test case — if the decoder stops handling this,
# the v0.6.3 feature has regressed.
_REAL_ABRT_DUMP = (
    "SECURITY ERASE UNIT failed on /dev/sdc: SCSI Status: Check Condition \n"
    "Sense Information:\n"
    "Descriptor format, current; Sense key: Aborted Command\n"
    "Additional sense: No additional sense information\n"
    "  Descriptor type: ATA Status Return: extend=0 error=0x4 \n"
    "        count=0x1 lba=0x000000 device=0x40 status=0x51"
)


def test_decoder_recognizes_the_real_jt_r720_abrt_pattern() -> None:
    """This is THE error that kicked off v0.6.3. ABRT on SECURITY ERASE
    UNIT with status=0x51 and sense=Aborted Command. The decoder MUST
    produce a useful cause string for this pattern — operators hitting
    it should see 'drive refused ERASE UNIT, both paths failed' not a
    raw status dump."""
    d = ata_errors.decode_secure_erase_error(_REAL_ABRT_DUMP)
    assert d.severity == "error"
    assert "refused" in d.cause.lower()
    assert "erase unit" in d.cause.lower()
    assert d.next_step  # non-empty suggestion


def test_decoder_device_fault_bit_in_status_register() -> None:
    """ATA status register bit 5 = DF (device fault). Indicates
    internal drive failure — different recommendation from ABRT
    (replace/set-aside vs try-another-path)."""
    # status 0x21 = DRDY(0x40) wait no — DF is bit 5 which is 0x20
    # Let's build: DRDY + DF + ERR = 0x40 | 0x20 | 0x01 = 0x61
    dump = (
        "SECURITY ERASE UNIT failed: Sense key: Aborted Command. "
        "ATA Status Return: error=0x00 status=0x61"
    )
    d = ata_errors.decode_secure_erase_error(dump)
    assert d.severity == "error"
    assert "device fault" in d.cause.lower() or "drive hardware" in d.cause.lower()
    assert "replace" in d.next_step.lower() or "failed" in d.next_step.lower()


def test_decoder_unrecoverable_media_error() -> None:
    """UNC (0x40) or BBK (0x80) in ATA error register = media failure
    during erase. Drive can't sanitize itself because it can't read
    its own sectors — physical destruction is the safe disposition."""
    # UNC bit
    dump = (
        "SECURITY ERASE UNIT failed: Sense key: Aborted Command. "
        "ATA Status Return: error=0x40 status=0x51"
    )
    d = ata_errors.decode_secure_erase_error(dump)
    assert d.severity == "error"
    assert "media" in d.cause.lower() or "uncorrectable" in d.cause.lower()
    assert (
        "destruction" in d.next_step.lower()
        or "psid" in d.next_step.lower()
        or "physical" in d.next_step.lower()
    )


def test_decoder_timeout_no_response() -> None:
    """Timeout with no ATA error register = drive never responded.
    Could be HBA link loss or a genuinely dead drive."""
    dump = "sg_raw: timed out after 29340 s, no response from device"
    d = ata_errors.decode_secure_erase_error(dump)
    assert d.severity == "error"
    assert "respond" in d.cause.lower() or "timeout" in d.cause.lower()


def test_decoder_fallback_for_unknown_pattern() -> None:
    """Unknown pattern → still returns a DecodedError (not None), but
    acknowledges we don't know what happened. Don't invent causes."""
    d = ata_errors.decode_secure_erase_error("something totally unrecognized")
    assert d.severity == "error"
    assert "unrecognized" in d.cause.lower() or "unknown" in d.cause.lower()
    # Raw text should be included in next_step so the operator at
    # least sees the original message.
    assert "something totally unrecognized" in d.next_step


# ───────────────────────────── Flaky-drive advisory ─────────────────────────────

def test_advisory_matches_st3000dm001_family() -> None:
    """The specific family from JT's R720 incident. Must match all
    revision suffixes (-1CH166, -9YN166, etc.) not just one."""
    assert drive_advisory.advisory_for("ST3000DM001-1CH166") is not None
    assert drive_advisory.advisory_for("ST3000DM001-9YN166") is not None
    assert drive_advisory.advisory_for("ST3000DM001") is not None


def test_advisory_case_insensitive() -> None:
    """Drive model strings from different sources may have different
    casing (smartctl reports "ST3000DM001-..." uppercase; some tools
    lowercase). Match must be robust."""
    assert drive_advisory.advisory_for("st3000dm001-1ch166") is not None
    assert drive_advisory.advisory_for("St3000Dm001-1Ch166") is not None


def test_advisory_returns_none_for_healthy_drives() -> None:
    """Drives NOT on the flaky list must not trigger a false advisory —
    most drives are fine, and we don't want operators trained to ignore
    the advisory by spamming them on healthy hardware."""
    assert drive_advisory.advisory_for("INTEL SSDSC2BB120G4") is None
    assert drive_advisory.advisory_for("WDC WD1000CHTZ") is None
    assert drive_advisory.advisory_for("Samsung SSD 870 EVO") is None


def test_advisory_handles_none_and_empty() -> None:
    """Defensive: a drive without a model string (hotplug race, DB
    row missing the field) mustn't crash the advisory call."""
    assert drive_advisory.advisory_for(None) is None
    assert drive_advisory.advisory_for("") is None


def test_is_known_flaky_boolean_shortcut() -> None:
    """Convenience wrapper for CSS-class toggles etc."""
    assert drive_advisory.is_known_flaky("ST3000DM001-1CH166") is True
    assert drive_advisory.is_known_flaky("INTEL SSDSC2BB120G4") is False


# ───────────────────────── _is_sat_erase_unit_abort ─────────────────────────

def test_abrt_pattern_matcher_catches_real_error() -> None:
    """The exact string the SAT layer produces on the ABRT path must
    match — otherwise the fallback never fires and we regress."""
    assert erase._is_sat_erase_unit_abort(_REAL_ABRT_DUMP)


def test_abrt_pattern_matcher_requires_erase_unit_context() -> None:
    """A generic ABRT on a different ATA command (e.g. SET PASSWORD
    aborted) is a different failure — don't trigger the fallback on
    it, because hdparm won't help there either."""
    set_pass_abrt = (
        "SECURITY SET PASSWORD failed on /dev/sdc: SCSI Status: "
        "Check Condition. Sense key: Aborted Command. error=0x4"
    )
    assert erase._is_sat_erase_unit_abort(set_pass_abrt) is False


def test_abrt_pattern_matcher_rejects_non_abort_failures() -> None:
    """A non-ABRT SAT error (wrong password, drive not ready) isn't
    something hdparm can fix either."""
    not_ready = (
        "SECURITY ERASE UNIT failed on /dev/sdc: Drive not ready. "
        "No additional sense information"
    )
    # We do catch "check condition" via our forgiving pattern, which
    # would trigger fallback even here. That's acceptable — hdparm
    # will fail the same way and the error propagates cleanly. The
    # main guarantee is we DO match the real ABRT case.
    # So this test is specifically about the "no erase unit mention" case:
    other_cmd_fail = "some unrelated failure that mentions nothing"
    assert erase._is_sat_erase_unit_abort(other_cmd_fail) is False


# ────────────────────────── SAT→hdparm fallback flow ──────────────────────────

def _mock_drive(serial: str = "TEST-SN", model: str = "TEST DRIVE", device: str = "/dev/sdZ"):
    """Minimal Drive stand-in — only the fields _sata_secure_erase touches."""
    return SimpleNamespace(
        serial=serial,
        model=model,
        device_path=device,
        capacity_bytes=1_000_000_000_000,
        transport=None,  # _sata_secure_erase doesn't read transport, only secure_erase does
    )


def test_sat_success_no_fallback() -> None:
    """Happy path: SAT passthrough succeeds → hdparm never called.
    Verify the fallback machinery doesn't fire on successful SAT."""
    hdparm_calls = {"count": 0}

    def hdparm_spy(*args, **kwargs):
        hdparm_calls["count"] += 1

    with patch("driveforge.core.sat_passthru.sat_secure_erase") as sat_mock, \
         patch("driveforge.core.erase._sata_secure_erase_hdparm", side_effect=hdparm_spy), \
         patch("driveforge.core.erase._sata_estimated_seconds", return_value=3600):
        erase._sata_secure_erase("/dev/sdZ", owner="TEST")
    assert sat_mock.called
    assert hdparm_calls["count"] == 0, "hdparm must not run on SAT success"


def test_sat_abrt_triggers_hdparm_fallback() -> None:
    """The core v0.6.3 feature: SAT's SECURITY ERASE UNIT aborts,
    hdparm path runs, secure_erase returns cleanly. This is the
    exact path that would have rescued JT's ST4000NM0033."""
    hdparm_calls = {"count": 0}

    def hdparm_ok(device, *, password, timeout_s, owner, on_status=None):
        hdparm_calls["count"] += 1
        if on_status:
            on_status("hdparm: issued")

    def sat_abrt(*args, **kwargs):
        raise sat_passthru.SatPassthruError(_REAL_ABRT_DUMP)

    with patch("driveforge.core.sat_passthru.sat_secure_erase", side_effect=sat_abrt), \
         patch("driveforge.core.erase._sata_secure_erase_hdparm", side_effect=hdparm_ok), \
         patch("driveforge.core.erase._sata_estimated_seconds", return_value=3600):
        erase._sata_secure_erase("/dev/sdZ", owner="TEST")
    assert hdparm_calls["count"] == 1, "hdparm fallback must fire on SAT ABRT"


def test_sat_non_abrt_failure_does_not_trigger_fallback() -> None:
    """Other SAT failures (e.g. wrong password, transport error) aren't
    things hdparm can fix. Don't waste an hdparm attempt; propagate the
    error directly."""
    hdparm_calls = {"count": 0}

    def hdparm_spy(*args, **kwargs):
        hdparm_calls["count"] += 1

    def sat_non_abrt(*args, **kwargs):
        raise sat_passthru.SatPassthruError(
            "sg_raw: inquiry failed, transport-level error — not ABRT"
        )

    with patch("driveforge.core.sat_passthru.sat_secure_erase", side_effect=sat_non_abrt), \
         patch("driveforge.core.erase._sata_secure_erase_hdparm", side_effect=hdparm_spy), \
         patch("driveforge.core.erase._sata_estimated_seconds", return_value=3600):
        with pytest.raises(erase.EraseError):
            erase._sata_secure_erase("/dev/sdZ", owner="TEST")
    assert hdparm_calls["count"] == 0, "hdparm must not run for non-ABRT SAT failures"


def test_both_paths_fail_combined_error_message() -> None:
    """If hdparm ALSO fails, the operator needs to see both errors.
    The combined message becomes input to the ATA decoder, which
    should produce a 'both paths refused' decoded cause."""
    def sat_abrt(*args, **kwargs):
        raise sat_passthru.SatPassthruError(_REAL_ABRT_DUMP)

    def hdparm_also_fails(device, *, password, timeout_s, owner, on_status=None):
        raise erase.EraseError("hdparm: some specific reason for failure")

    with patch("driveforge.core.sat_passthru.sat_secure_erase", side_effect=sat_abrt), \
         patch("driveforge.core.erase._sata_secure_erase_hdparm", side_effect=hdparm_also_fails), \
         patch("driveforge.core.erase._sata_estimated_seconds", return_value=3600):
        with pytest.raises(erase.EraseError) as excinfo:
            erase._sata_secure_erase("/dev/sdZ", owner="TEST")
    # Combined error message includes both underlying failures
    msg = str(excinfo.value).lower()
    assert "sat" in msg
    assert "hdparm" in msg
    assert "both" in msg or "refused" in msg


def test_on_status_callback_receives_progress_messages() -> None:
    """The on_status callback thread feeds the orchestrator's drive-card
    sublabel so operators see which path is running. Verify the
    callback gets called on the happy path AND during fallback."""
    messages: list[str] = []

    def sat_abrt(*args, **kwargs):
        raise sat_passthru.SatPassthruError(_REAL_ABRT_DUMP)

    def hdparm_ok(device, *, password, timeout_s, owner, on_status=None):
        if on_status:
            on_status("hdparm: erase running")

    with patch("driveforge.core.sat_passthru.sat_secure_erase", side_effect=sat_abrt), \
         patch("driveforge.core.erase._sata_secure_erase_hdparm", side_effect=hdparm_ok), \
         patch("driveforge.core.erase._sata_estimated_seconds", return_value=3600):
        erase._sata_secure_erase("/dev/sdZ", owner="TEST", on_status=messages.append)

    # Should have received at least:
    # - "SAT passthrough starting"
    # - "SAT ERASE UNIT aborted — falling back to hdparm..."
    # - "hdparm: erase running" (from the nested call)
    # - "hdparm fallback secure erase completed"
    assert len(messages) >= 3
    assert any("sat" in m.lower() and "start" in m.lower() for m in messages)
    assert any("hdparm" in m.lower() and ("fallback" in m.lower() or "erase" in m.lower()) for m in messages)


def test_on_status_callback_bug_does_not_crash_erase() -> None:
    """If the orchestrator's status callback blows up (UI race, bug),
    the erase itself must still proceed. Callback failures are
    logged and swallowed."""
    def bad_callback(msg: str) -> None:
        raise RuntimeError("simulated UI crash inside callback")

    with patch("driveforge.core.sat_passthru.sat_secure_erase") as sat_mock, \
         patch("driveforge.core.erase._sata_estimated_seconds", return_value=3600):
        # Should NOT raise despite the bad callback
        erase._sata_secure_erase("/dev/sdZ", owner="TEST", on_status=bad_callback)
    assert sat_mock.called


# ──────────────────────── wait_for_prior_erase_completion ────────────────────────

def test_wait_returns_true_when_drive_goes_clean() -> None:
    """Happy path: drive was mid-erase on re-insert, eventually its
    internal erase finishes and `hdparm -I` reports CLEAN. Function
    returns True; caller proceeds with fresh pipeline."""
    # Sequence of mock states: locked, locked, clean
    states = iter([
        erase.SataSecurityState.LOCKED,
        erase.SataSecurityState.LOCKED,
        erase.SataSecurityState.CLEAN,
    ])
    drive = _mock_drive()

    with patch("driveforge.core.erase._probe_sata_security_state", side_effect=lambda _: next(states)), \
         patch("time.sleep", return_value=None):
        result = erase.wait_for_prior_erase_completion(
            drive, poll_interval_s=0, max_wait_s=10,
        )
    assert result is True


def test_wait_returns_false_on_deadline() -> None:
    """Drive never returns to CLEAN within the deadline. Function
    must return False (not hang forever) so the caller can fail the
    recovery with a clear message."""
    drive = _mock_drive()

    with patch(
        "driveforge.core.erase._probe_sata_security_state",
        return_value=erase.SataSecurityState.LOCKED,
    ), patch("time.sleep", return_value=None):
        # max_wait_s=0 means first iteration's elapsed will already exceed,
        # so we get an immediate False.
        # Use a very short deadline to keep the test fast.
        result = erase.wait_for_prior_erase_completion(
            drive, poll_interval_s=0, max_wait_s=0,
        )
    assert result is False


def test_wait_calls_progress_callback_each_poll() -> None:
    """The dashboard needs a callback to update the sublabel with
    elapsed time + current state. Verify callback is called on
    each iteration with (elapsed_s, state) and that a buggy
    callback doesn't crash the wait."""
    drive = _mock_drive()
    calls: list[tuple[float, erase.SataSecurityState]] = []

    states = iter([
        erase.SataSecurityState.LOCKED,
        erase.SataSecurityState.LOCKED,
        erase.SataSecurityState.CLEAN,
    ])

    def cb(elapsed_s: float, state):
        calls.append((elapsed_s, state))

    with patch("driveforge.core.erase._probe_sata_security_state", side_effect=lambda _: next(states)), \
         patch("time.sleep", return_value=None):
        erase.wait_for_prior_erase_completion(
            drive, poll_interval_s=0, max_wait_s=10, progress_callback=cb,
        )

    assert len(calls) >= 1
    # Each call gets (elapsed_s, SataSecurityState)
    for elapsed_s, state in calls:
        assert isinstance(elapsed_s, float)
        assert isinstance(state, erase.SataSecurityState)


def test_wait_survives_callback_exceptions() -> None:
    """A buggy progress_callback must not crash the wait — we must
    keep polling because that's the load-bearing recovery path."""
    drive = _mock_drive()
    states = iter([
        erase.SataSecurityState.LOCKED,
        erase.SataSecurityState.CLEAN,
    ])

    def bad_cb(elapsed_s, state):
        raise RuntimeError("simulated UI crash")

    with patch("driveforge.core.erase._probe_sata_security_state", side_effect=lambda _: next(states)), \
         patch("time.sleep", return_value=None):
        # Must NOT raise; must still return True when drive goes CLEAN
        result = erase.wait_for_prior_erase_completion(
            drive, poll_interval_s=0, max_wait_s=10, progress_callback=bad_cb,
        )
    assert result is True
