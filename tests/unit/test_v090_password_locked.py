"""v0.9.0 — password-locked-drive detector + factory-master auto-retry +
remediation state module + HTTP routes.

Real-world case that motivated this: JT's WD Blue 1TB
(WD-WCC3F5XC2452) came password-locked by some prior host. The
default DriveForge password didn't unlock it, and pre-v0.9.0 the
pipeline just died as `error` with no structured recovery UI.
v0.9.0 adds:

  - `is_security_locked_pattern()` for orchestrator routing
  - `_vendor_factory_master_for(model)` + `_try_factory_master_erase()`
    for auto-recovery when master-password-revision = 65534
  - `password_locked_remediation` state module (same shape as
    v0.6.9 frozen_remediation)
  - HTTP routes: /drives/<serial>/password-locked/try-unlock and
    /drives/<serial>/password-locked/mark-unrecoverable
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.core import erase
from driveforge.core import password_locked_remediation as pwd_lock
from driveforge.core.password_locked_remediation import (
    PasswordLockedState,
    PasswordLockedStatus,
    REMEDIATION_STEPS,
    clear,
    record_manual_attempt,
    register_locked,
)
from driveforge.core.process import ProcessResult


# -------------------------------------------------- detector


def test_is_security_locked_pattern_matches_v090_error_text() -> None:
    """The v0.9.0 preflight raise-text must trigger the detector so
    the orchestrator knows to route to the remediation panel. This
    is the 'downstream compatibility' test — if the error text
    changes without updating this test, the orchestrator would stop
    registering locked drives and the feature would silently break."""
    real_error = (
        "preflight: drive /dev/sdz is security-locked with an unknown password. "
        "DriveForge's default password ('driveforge') did not unlock it, and "
        "the vendor-factory-master SECURITY ERASE ENHANCED recovery path also "
        "failed (no known factory master password for vendor). Operator "
        "remediation required — see the drive-detail page for the PSID-revert "
        "/ manual-password / mark-unrecoverable options. Underlying unlock "
        "error: SECURITY UNLOCK failed on /dev/sdz"
    )
    assert erase.is_security_locked_pattern(real_error) is True


def test_is_security_locked_pattern_does_not_match_other_errors() -> None:
    """Must NOT fire on the libata-freeze pattern (different failure
    class — handled by `is_libata_freeze_pattern`) or on generic
    subprocess errors. False positives here would misroute freeze-
    pattern drives into the password-locked panel."""
    freeze_error = (
        "Drive refused SECURITY ERASE UNIT over both SAT passthrough AND "
        "native hdparm ATA paths with ABRT"
    )
    assert erase.is_security_locked_pattern(freeze_error) is False

    other_error = "sg_raw: hardware I/O error on /dev/sdz"
    assert erase.is_security_locked_pattern(other_error) is False


# -------------------------------------------------- factory-master lookup


def test_vendor_master_matches_wd_drives() -> None:
    """WD model prefixes (WDC, WD) map to the WD factory master
    password. This is the most common case — WD Blue drives pulled
    from laptops with BIOS HDD passwords."""
    wd_master = "WDCWDCWDCWDCWDCWDCWDCWDCWDCWDCWD"
    assert erase._vendor_factory_master_for("WDC WD10EZEX-60M2NA0") == wd_master
    assert erase._vendor_factory_master_for("WD Black WD1003FZEX") == wd_master


def test_vendor_master_matches_seagate_toshiba_hgst() -> None:
    """Seagate / Toshiba / HGST default to 32 null bytes historically.
    Matching on model prefix is sufficient."""
    zero32 = "\x00" * 32
    assert erase._vendor_factory_master_for("ST3000DM001-1CH166") == zero32
    assert erase._vendor_factory_master_for("Seagate Exos 7E8") == zero32
    assert erase._vendor_factory_master_for("TOSHIBA MG08ADA400E") == zero32
    assert erase._vendor_factory_master_for("HGST HUS726T6TALE6L4") == zero32


def test_vendor_master_returns_none_for_unknown_vendor() -> None:
    """Unknown vendor (e.g. fresh test fixture) → None. We must NOT
    guess across vendors; every wrong attempt burns one of the
    drive's ~5-strike lockout counter slots."""
    assert erase._vendor_factory_master_for("MysteryBrand X1") is None
    assert erase._vendor_factory_master_for("TEST-MODEL") is None
    assert erase._vendor_factory_master_for(None) is None
    assert erase._vendor_factory_master_for("") is None


def test_is_master_at_factory_default_parses_hdparm_output() -> None:
    """hdparm -I's master-revision line is the signal we use to
    decide whether factory-default master will work. Revision 65534
    = factory; anything else = changed. Parser must handle both the
    literal smartctl output line + case variations."""
    factory_stdout = (
        "Security:\n"
        "\tMaster password revision code = 65534\n"
        "\t\tsupported\n"
        "\t\tenabled\n"
        "\t\tlocked\n"
    )
    assert erase._is_master_password_at_factory_default(factory_stdout) is True

    changed_stdout = (
        "Security:\n"
        "\tMaster password revision code = 42\n"
        "\t\tsupported\n"
        "\t\tenabled\n"
    )
    assert erase._is_master_password_at_factory_default(changed_stdout) is False

    empty_stdout = "Security:\n\tnot enabled\n"
    assert erase._is_master_password_at_factory_default(empty_stdout) is False


# ------------------------------------------- factory-master erase attempt


def test_try_factory_master_erase_returns_early_for_unknown_vendor() -> None:
    """Unknown vendor → early return with (False, reason). No hdparm
    call made — operator's log clearly says why we skipped."""
    ok, msg = erase._try_factory_master_erase(
        "/dev/sdz", model="UnknownBrand", owner="test-serial",
    )
    assert ok is False
    assert "no known factory master" in msg


def test_try_factory_master_erase_skips_when_master_revision_changed(
    monkeypatch,
) -> None:
    """Master revision != 65534 → skip the attempt. We'd waste a
    lockout strike for a password that won't work."""
    def fake_run(argv, **kwargs):
        # `hdparm -I` probe — return a version where revision is
        # CHANGED from factory.
        return ProcessResult(
            argv=argv, returncode=0,
            stdout=(
                "Security:\n"
                "\tMaster password revision code = 42\n"
                "\t\tenabled\n"
                "\t\tlocked\n"
            ),
            stderr="",
        )
    monkeypatch.setattr(erase, "run", fake_run)

    ok, msg = erase._try_factory_master_erase(
        "/dev/sdz", model="WDC WD10EZEX", owner="test",
    )
    assert ok is False
    assert "master password is NOT at factory default" in msg
    assert "lockout strike" in msg


def test_try_factory_master_erase_success_returns_ok(monkeypatch) -> None:
    """Factory master password successfully erases the drive →
    returns (True, message). Caller can proceed with pipeline from
    CLEAN state."""
    call_count = {"n": 0}

    def fake_run(argv, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call: hdparm -I probe
            return ProcessResult(
                argv=argv, returncode=0,
                stdout=(
                    "Security:\n"
                    "\tMaster password revision code = 65534\n"
                    "\t\tsupported\n"
                    "\t\tenabled\n"
                    "\t\tlocked\n"
                ),
                stderr="",
            )
        # Second call: hdparm --security-erase-enhanced
        return ProcessResult(
            argv=argv, returncode=0,
            stdout="security_password: ****\n" "Issuing SECURITY_ERASE command\n",
            stderr="",
        )
    monkeypatch.setattr(erase, "run", fake_run)

    ok, msg = erase._try_factory_master_erase(
        "/dev/sdz", model="WDC WD10EZEX-60M2NA0", owner="test",
    )
    assert ok is True
    assert "succeeded" in msg
    # Must have made exactly 2 subprocess calls
    assert call_count["n"] == 2


def test_try_factory_master_erase_failure_returns_helpful_error(
    monkeypatch,
) -> None:
    """hdparm returned non-zero → factory master was wrong for this
    specific drive (unusual but possible). Surface the stderr so
    the operator can see what happened."""
    def fake_run(argv, **kwargs):
        if "-I" in argv:
            return ProcessResult(
                argv=argv, returncode=0,
                stdout="Master password revision code = 65534\n",
                stderr="",
            )
        # security-erase-enhanced — simulated rejection
        return ProcessResult(
            argv=argv, returncode=1,
            stdout="",
            stderr="SECURITY_ERASE_PREPARE: input/output error\n",
        )
    monkeypatch.setattr(erase, "run", fake_run)

    ok, msg = erase._try_factory_master_erase(
        "/dev/sdz", model="WDC WD10EZEX", owner="test",
    )
    assert ok is False
    assert "vendor-factory-master" in msg
    assert "rc=1" in msg


# -------------------------------------------- remediation state module


def test_register_locked_first_call_sets_needs_action() -> None:
    frozen: dict = {}
    state = register_locked(
        frozen, serial="SN-1", drive_model="WDC WD10EZEX",
    )
    assert state.serial == "SN-1"
    assert state.status is PasswordLockedStatus.NEEDS_ACTION
    assert state.retry_count == 0
    assert state.manual_attempts == 0


def test_register_locked_second_call_escalates_status() -> None:
    """Operator hit the panel, tried remediation, failed, came back
    locked again → status escalates to RETRIED_STILL_LOCKED so the
    panel tone shifts toward destruction."""
    locked: dict = {}
    first = register_locked(locked, serial="SN-2", drive_model="WD Blue")
    second = register_locked(locked, serial="SN-2", drive_model="WD Blue")

    assert second is first  # same entry mutated
    assert second.retry_count == 1
    assert second.status is PasswordLockedStatus.RETRIED_STILL_LOCKED


def test_record_manual_attempt_bumps_counter_and_stores_note() -> None:
    """Each manual-password try via the UI bumps the counter.
    Template renders attempts_remaining_estimate to warn the
    operator before the drive permanently locks out."""
    locked: dict = {}
    register_locked(locked, serial="SN-3", drive_model="WD")

    result = record_manual_attempt(
        locked, "SN-3", ok=False, note="wrong password",
    )
    assert result is not None
    assert result.manual_attempts == 1
    assert result.last_attempt_note == "wrong password"
    assert result.attempts_remaining_estimate == 4

    # Second failed attempt
    record_manual_attempt(locked, "SN-3", ok=False, note="still wrong")
    assert locked["SN-3"].manual_attempts == 2
    assert locked["SN-3"].attempts_remaining_estimate == 3


def test_clear_removes_entry_and_is_idempotent() -> None:
    locked: dict = {}
    register_locked(locked, serial="SN-4", drive_model="x")
    assert "SN-4" in locked
    clear(locked, "SN-4")
    assert "SN-4" not in locked
    # idempotent
    clear(locked, "SN-4")
    clear(locked, "never-registered")


def test_remediation_steps_list_is_ordered_least_to_most_invasive() -> None:
    """Panel renders these in order. PSID (cheapest — cryptographic)
    first, destroy (most invasive) last. Matches frozen_remediation's
    convention."""
    kinds = [s.kind for s in REMEDIATION_STEPS]
    assert kinds[0] == "psid_revert"
    assert kinds[-1] == "destroy"
    # Sanity: each step has title + detail
    for s in REMEDIATION_STEPS:
        assert s.title
        assert s.detail


def test_attempts_remaining_estimate_never_goes_negative() -> None:
    """Defensive: even if somehow manual_attempts exceeds 5, the
    estimate clamps to 0 (not -N). Prevents weird template output."""
    state = PasswordLockedState(
        serial="X",
        drive_model="m",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        manual_attempts=10,
    )
    assert state.attempts_remaining_estimate == 0


# ------------------------------------------------------- HTTP routes


def _bootstrap_app(tmp_path):
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True
    app = make_app(settings)
    DaemonState.boot(settings)
    return app


def test_try_unlock_route_rejects_empty_password(tmp_path) -> None:
    app = _bootstrap_app(tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/drives/SN-X/password-locked/try-unlock",
            data={"password": ""},
        )
    assert resp.status_code == 303
    assert "pwd_error=" in resp.headers["location"]
    assert "empty" in resp.headers["location"].lower()


def test_try_unlock_route_rejects_when_drive_not_present(tmp_path) -> None:
    """Drive not in device_basenames → refuse with a clear flash.
    hdparm must not run against a non-existent device."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    # Pre-register the lock state but don't populate device_basenames
    register_locked(state.password_locked, serial="SN-GONE", drive_model="WD")

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/drives/SN-GONE/password-locked/try-unlock",
            data={"password": "something"},
        )
    assert resp.status_code == 303
    assert "not+currently+plugged" in resp.headers["location"] or \
           "not%20currently%20plugged" in resp.headers["location"]


def test_mark_unrecoverable_route_stamps_f_and_clears_state(tmp_path) -> None:
    """Clicking Mark as unrecoverable stamps F on the latest TestRun
    (creates one if none) + clears the in-memory remediation entry.
    F is sticky so auto-enroll skips on future inserts."""
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m

    app = _bootstrap_app(tmp_path)
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="SN-DESTROY",
            model="WD Blue",
            capacity_bytes=1_000_000_000_000,
            transport="sata",
        ))
        session.commit()
    register_locked(state.password_locked, serial="SN-DESTROY", drive_model="WD Blue")

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/drives/SN-DESTROY/password-locked/mark-unrecoverable")

    assert resp.status_code == 303
    assert "pwd_ok=" in resp.headers["location"]
    assert "SN-DESTROY" not in state.password_locked
    # F grade stamped on a fresh TestRun row
    with state.session_factory() as session:
        run = (
            session.query(m.TestRun)
            .filter_by(drive_serial="SN-DESTROY")
            .order_by(m.TestRun.completed_at.desc())
            .first()
        )
    assert run is not None
    assert run.grade == "F"
    assert run.phase == "password_locked_unrecoverable"
    assert run.error_message and "unknown password" in run.error_message.lower()
