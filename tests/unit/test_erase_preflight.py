"""Tests for the v0.5.0 self-healing secure_erase pre-flight.

`ensure_clean_security_state(drive)` is the new guarantee: every
`secure_erase` phase starts by making sure the drive is in a sane
security state. Self-heal what's recoverable; refuse with a clear
user-facing message on what's not.

States covered:
  - CLEAN         — pass through, nothing to do
  - ENABLED       — our previous run's leftover password; DISABLE, start fresh
  - LOCKED        — post-power-cycle enabled state; UNLOCK, DISABLE, start fresh
  - FROZEN        — BIOS-induced; raise clear EraseError
  - UNKNOWN       — hdparm -I unresponsive; raise clear EraseError
  - ENABLED/LOCKED with an unknown password — SAT unlock/disable fails;
    raise clear EraseError with remediation options

Plus: no-op on SAS and NVMe transports (no ATA security state).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from driveforge.core import erase, sat_passthru
from driveforge.core.drive import Drive, Transport
from driveforge.core.process import ProcessResult


def _drive(transport: Transport = Transport.SATA) -> Drive:
    return Drive(
        serial="SN-PREFLIGHT-1",
        model="TEST-MODEL",
        capacity_bytes=1_000_000_000,
        transport=transport,
        device_path="/dev/sdz",
        rotation_rate=0,
    )


# ---------------------------------------------------------------- state parser


def test_parse_clean_state() -> None:
    """hdparm -I stanza where all three are `not` — pristine drive."""
    output = """
Security:
\tMaster password revision code = 65534
\tsupported
\tnot\tenabled
\tnot\tlocked
\tnot\tfrozen
\tnot\texpired: security count
"""
    assert erase._parse_sata_security_state(output) == erase.SataSecurityState.CLEAN


def test_parse_enabled_state() -> None:
    """hdparm shows `enabled` without the `not` prefix."""
    output = """
Security:
\tMaster password revision code = 65534
\tsupported
\tenabled
\tnot\tlocked
\tnot\tfrozen
"""
    assert erase._parse_sata_security_state(output) == erase.SataSecurityState.ENABLED


def test_parse_locked_state() -> None:
    """Drive has security enabled AND is currently locked (post-power-cycle
    state when a password was set before the cycle)."""
    output = """
Security:
\tsupported
\tenabled
\tlocked
\tnot\tfrozen
"""
    assert erase._parse_sata_security_state(output) == erase.SataSecurityState.LOCKED


def test_parse_frozen_beats_other_states() -> None:
    """FROZEN takes precedence — a frozen drive reports enabled too,
    but what matters operationally is that we can't do anything
    security-side until the freeze is cleared."""
    output = """
Security:
\tsupported
\tenabled
\tnot\tlocked
\tfrozen
"""
    assert erase._parse_sata_security_state(output) == erase.SataSecurityState.FROZEN


def test_parse_word_substring_not_confused() -> None:
    """The tab-prefix anchor prevents 'not enabled' from matching 'enabled'.
    Regression guard: a naive substring match would read the word 'enabled'
    in 'not\tenabled' and flag the drive as enabled incorrectly."""
    output = "\tnot\tenabled\n\tnot\tlocked\n\tnot\tfrozen"
    assert erase._parse_sata_security_state(output) == erase.SataSecurityState.CLEAN


# ---------------------------------------------------------------- non-SATA no-op


def test_preflight_noop_on_sas(monkeypatch) -> None:
    """SAS drives have no ATA security state — preflight must be a no-op.
    Verify by confirming no hdparm -I call is made."""
    drive = _drive(transport=Transport.SAS)
    # detect_true_transport for a genuine SAS drive returns SAS → stays SAS
    monkeypatch.setattr(
        "driveforge.core.drive.detect_true_transport",
        lambda _dev: Transport.SAS,
    )
    called = []
    def fake_run(argv, **kwargs):
        called.append(argv)
        return ProcessResult(argv=argv, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(erase, "run", fake_run)
    erase.ensure_clean_security_state(drive)
    assert called == [], "preflight must not invoke any command on SAS drives"


def test_preflight_noop_on_nvme(monkeypatch) -> None:
    """NVMe format is atomic crypto-erase; no pre-flight needed."""
    drive = _drive(transport=Transport.NVME)
    called = []
    def fake_run(argv, **kwargs):
        called.append(argv)
        return ProcessResult(argv=argv, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(erase, "run", fake_run)
    erase.ensure_clean_security_state(drive)
    assert called == []


def test_preflight_refines_sas_to_sata(monkeypatch) -> None:
    """SATA-on-SAS: lsblk reports tran=sas but smartctl reveals SATA.
    Preflight must refine via detect_true_transport so SATA state-repair
    actually fires."""
    drive = _drive(transport=Transport.SAS)
    # Override refine to report SATA (SATA-on-SAS case)
    monkeypatch.setattr(
        "driveforge.core.drive.detect_true_transport",
        lambda _dev: Transport.SATA,
    )
    # Drive is CLEAN — preflight returns without doing anything
    def fake_run(argv, **kwargs):
        if argv[0] == "hdparm" and argv[1] == "-I":
            return ProcessResult(
                argv=argv, returncode=0,
                stdout="Security:\n\tnot\tenabled\n\tnot\tlocked\n\tnot\tfrozen\n",
                stderr="",
            )
        return ProcessResult(argv=argv, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(erase, "run", fake_run)
    # Should complete without error
    erase.ensure_clean_security_state(drive)


# ---------------------------------------------------------------- CLEAN passthrough


def test_preflight_clean_state_is_fast_passthrough(monkeypatch) -> None:
    """CLEAN drives: probe, return immediately. No SAT calls."""
    drive = _drive()
    sat_called = []
    def spy(device, **kwargs):
        sat_called.append(("unlock_or_disable", device))
    monkeypatch.setattr(sat_passthru, "security_unlock", spy)
    monkeypatch.setattr(sat_passthru, "security_disable_password", spy)

    def fake_run(argv, **kwargs):
        return ProcessResult(
            argv=argv, returncode=0,
            stdout="Security:\n\tnot\tenabled\n\tnot\tlocked\n\tnot\tfrozen\n",
            stderr="",
        )
    monkeypatch.setattr(erase, "run", fake_run)

    erase.ensure_clean_security_state(drive)
    assert sat_called == [], "CLEAN state must skip all SAT calls"


# ---------------------------------------------------------------- ENABLED auto-heal


def test_preflight_enabled_self_heals_via_disable(monkeypatch) -> None:
    """ENABLED state (leftover password from a previous run) → call
    DISABLE with our default password, complete without raising."""
    drive = _drive()
    disabled = []
    def spy_disable(device, **kwargs):
        disabled.append((device, kwargs))
    monkeypatch.setattr(sat_passthru, "security_disable_password", spy_disable)
    monkeypatch.setattr(sat_passthru, "security_unlock", MagicMock())  # not called

    def fake_run(argv, **kwargs):
        return ProcessResult(
            argv=argv, returncode=0,
            stdout="Security:\n\tenabled\n\tnot\tlocked\n\tnot\tfrozen\n",
            stderr="",
        )
    monkeypatch.setattr(erase, "run", fake_run)
    erase.ensure_clean_security_state(drive)
    assert len(disabled) == 1, "ENABLED state must call DISABLE exactly once"
    assert disabled[0][0] == drive.device_path
    assert disabled[0][1].get("owner") == drive.serial


# ---------------------------------------------------------------- LOCKED auto-heal


def test_preflight_locked_self_heals_via_unlock_then_disable(monkeypatch) -> None:
    """LOCKED state (post-power-cycle) → UNLOCK with our password, then
    DISABLE to clear. Both must happen, in that order."""
    drive = _drive()
    call_order = []
    def spy_unlock(device, **kwargs):
        call_order.append("unlock")
    def spy_disable(device, **kwargs):
        call_order.append("disable")
    monkeypatch.setattr(sat_passthru, "security_unlock", spy_unlock)
    monkeypatch.setattr(sat_passthru, "security_disable_password", spy_disable)

    def fake_run(argv, **kwargs):
        return ProcessResult(
            argv=argv, returncode=0,
            stdout="Security:\n\tenabled\n\tlocked\n\tnot\tfrozen\n",
            stderr="",
        )
    monkeypatch.setattr(erase, "run", fake_run)
    erase.ensure_clean_security_state(drive)
    assert call_order == ["unlock", "disable"]


# ---------------------------------------------------------------- unrecoverable


def test_preflight_frozen_raises_clear_error(monkeypatch) -> None:
    """FROZEN: raise EraseError with user-facing explanation of BIOS
    freeze. Don't attempt any SAT call — that would just add noise."""
    drive = _drive()
    sat_called = []
    monkeypatch.setattr(
        sat_passthru, "security_unlock",
        lambda *a, **k: sat_called.append("unlock"),
    )
    monkeypatch.setattr(
        sat_passthru, "security_disable_password",
        lambda *a, **k: sat_called.append("disable"),
    )
    def fake_run(argv, **kwargs):
        return ProcessResult(
            argv=argv, returncode=0,
            stdout="Security:\n\tenabled\n\tnot\tlocked\n\tfrozen\n",
            stderr="",
        )
    monkeypatch.setattr(erase, "run", fake_run)
    with pytest.raises(erase.EraseError) as exc_info:
        erase.ensure_clean_security_state(drive)
    msg = str(exc_info.value)
    assert "FROZEN" in msg
    assert "BIOS" in msg or "SECURITY FREEZE LOCK" in msg
    assert sat_called == [], "FROZEN state must not attempt SAT commands"


def test_preflight_locked_with_unknown_password_raises_clear_error(monkeypatch) -> None:
    """LOCKED with a password we don't know → UNLOCK fails → raise
    EraseError with remediation options (boot on another system, SED
    PSID, replace)."""
    drive = _drive()
    def failing_unlock(device, **kwargs):
        raise sat_passthru.SatPassthruError(
            f"SECURITY UNLOCK failed on {device}: sg_raw non-zero"
        )
    monkeypatch.setattr(sat_passthru, "security_unlock", failing_unlock)
    def fake_run(argv, **kwargs):
        return ProcessResult(
            argv=argv, returncode=0,
            stdout="Security:\n\tenabled\n\tlocked\n\tnot\tfrozen\n",
            stderr="",
        )
    monkeypatch.setattr(erase, "run", fake_run)

    with pytest.raises(erase.EraseError) as exc_info:
        erase.ensure_clean_security_state(drive)
    msg = str(exc_info.value)
    # Remediation options must be surfaced — key phrases the operator
    # needs to read:
    assert "unknown password" in msg.lower()
    assert "boot the drive" in msg.lower() or "replace the drive" in msg.lower()


def test_preflight_enabled_with_undisable_able_password_raises(monkeypatch) -> None:
    """ENABLED but our password doesn't clear it (another tool set one
    we don't know) → raise EraseError pointing at the docs."""
    drive = _drive()
    def failing_disable(device, **kwargs):
        raise sat_passthru.SatPassthruError("SECURITY DISABLE PASSWORD failed")
    monkeypatch.setattr(sat_passthru, "security_disable_password", failing_disable)
    def fake_run(argv, **kwargs):
        return ProcessResult(
            argv=argv, returncode=0,
            stdout="Security:\n\tenabled\n\tnot\tlocked\n\tnot\tfrozen\n",
            stderr="",
        )
    monkeypatch.setattr(erase, "run", fake_run)

    with pytest.raises(erase.EraseError) as exc_info:
        erase.ensure_clean_security_state(drive)
    msg = str(exc_info.value)
    assert "security" in msg.lower()


def test_preflight_hdparm_failure_raises_unknown_state_error(monkeypatch) -> None:
    """hdparm -I failing (drive unresponsive, bus glitch) → raise
    EraseError rather than silently proceeding into an erase that'll
    likely fail."""
    drive = _drive()
    def failing_run(argv, **kwargs):
        return ProcessResult(
            argv=argv, returncode=2,
            stdout="",
            stderr="HDIO_GET_IDENTITY failed: Inappropriate ioctl for device",
        )
    monkeypatch.setattr(erase, "run", failing_run)

    with pytest.raises(erase.EraseError) as exc_info:
        erase.ensure_clean_security_state(drive)
    msg = str(exc_info.value)
    assert "security state" in msg.lower() or "hdparm" in msg.lower()
