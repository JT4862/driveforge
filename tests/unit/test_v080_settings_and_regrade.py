"""v0.8.0 — Settings UI save + Regrade route.

Covers:
  - POST /settings/grading persists all new v0.8.0 threshold fields
  - Checkbox semantics (unchecked = missing from form, treated as
    False) match how Starlette form encoding works
  - POST /drives/{serial}/regrade refusal paths (not present / active
    pipeline / no prior A/B/C run) all surface distinct flash params
  - Successful regrade creates a new TestRun(phase="regrade") with
    regrade_of_run_id pointing at the source
  - POST /regrade-all-idle iterates across idle drives
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from driveforge import config as cfg


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


# ------------------------------------------------ Settings save


def test_save_grading_persists_all_new_v080_fields(tmp_path) -> None:
    """Every new field the UI exposes must round-trip through the
    POST handler. Pre-v0.8.0 the save route only handled the old
    reallocated-max fields; a regression here would silently drop
    operator edits."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/settings/grading",
            data={
                # Existing fields
                "grade_a_reallocated_max": "5",
                "grade_b_reallocated_max": "10",
                "grade_c_reallocated_max": "50",
                "fail_on_pending_sectors": "on",
                "fail_on_offline_uncorrectable": "on",
                "thermal_excursion_c": "65",
                # v0.8.0 age ceilings
                "age_ceiling_enabled": "on",
                "poh_a_ceiling_hours": "40000",
                "poh_b_ceiling_hours": "70000",
                "poh_fail_hours": "100000",
                # v0.8.0 workload
                "workload_ceiling_enabled": "on",
                "workload_a_ceiling_pct": "65",
                "workload_b_ceiling_pct": "110",
                "workload_fail_pct": "160",
                "rated_tbw_enterprise_hdd": "3000",
                "rated_tbw_enterprise_ssd": "4000",
                "rated_tbw_consumer_hdd": "300",
                "rated_tbw_consumer_ssd": "700",
                # v0.8.0 SSD wear
                "ssd_wear_ceiling_enabled": "on",
                "ssd_wear_a_ceiling_pct": "25",
                "ssd_wear_b_ceiling_pct": "55",
                "ssd_wear_fail_pct": "95",
                "fail_on_low_nvme_spare": "on",
                # v0.8.0 error rules
                "error_rules_enabled": "on",
                "fail_on_end_to_end_error": "on",
                "fail_on_nvme_critical_warning": "on",
                "cap_c_on_nvme_media_errors": "on",
                "command_timeout_b_ceiling": "7",
                "cap_c_on_past_self_test_failure": "on",
            },
        )
    assert resp.status_code == 303
    g = state.settings.grading
    assert g.poh_a_ceiling_hours == 40000
    assert g.poh_b_ceiling_hours == 70000
    assert g.poh_fail_hours == 100000
    assert g.workload_a_ceiling_pct == 65
    assert g.workload_fail_pct == 160
    assert g.rated_tbw_enterprise_hdd == 3000
    assert g.rated_tbw_consumer_ssd == 700
    assert g.ssd_wear_fail_pct == 95
    assert g.fail_on_low_nvme_spare is True
    assert g.command_timeout_b_ceiling == 7


def test_save_grading_unchecked_boxes_become_false(tmp_path) -> None:
    """HTML form convention: an unchecked checkbox is absent from the
    POST body. The save route must interpret absence as False. Without
    this, the checkboxes would be write-only (you could enable but
    never disable via the UI)."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    # Start with everything enabled (defaults)
    assert state.settings.grading.age_ceiling_enabled is True

    with TestClient(app, follow_redirects=False) as client:
        # Submit form WITHOUT age_ceiling_enabled / workload_ceiling_enabled
        client.post(
            "/settings/grading",
            data={
                "grade_a_reallocated_max": "3",
                "grade_b_reallocated_max": "8",
                "grade_c_reallocated_max": "40",
                # (no checkboxes set)
            },
        )
    g = state.settings.grading
    assert g.age_ceiling_enabled is False
    assert g.workload_ceiling_enabled is False
    assert g.ssd_wear_ceiling_enabled is False
    assert g.error_rules_enabled is False
    assert g.fail_on_pending_sectors is False


def test_save_grading_blank_poh_fail_hours_parses_as_none(tmp_path) -> None:
    """The auto-fail POH is nullable (operator disables by blanking
    the field). Blank string must become None, not raise ValueError."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/settings/grading",
            data={
                "grade_a_reallocated_max": "3",
                "grade_b_reallocated_max": "8",
                "grade_c_reallocated_max": "40",
                "poh_fail_hours": "",  # blank
            },
        )
    assert resp.status_code == 303
    assert state.settings.grading.poh_fail_hours is None


# ---------------------------------------------------- Regrade route


def _seed_drive_and_run(state, serial, grade="A", model="Test Drive") -> int:
    """Insert a Drive + a completed TestRun and return the run_id."""
    from driveforge.db import models as m
    with state.session_factory() as session:
        session.add(m.Drive(
            serial=serial,
            model=model,
            capacity_bytes=1_000_000_000_000,
            transport="sata",
            rotational=False,
        ))
        run = m.TestRun(
            drive_serial=serial,
            phase="done",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            grade=grade,
            reallocated_sectors=0,
            power_on_hours_at_test=5000,
            throughput_mean_mbps=140.0,
            throughput_p5_mbps=130.0,
            throughput_p95_mbps=150.0,
            throughput_pass_means=[140.0] * 8,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def test_regrade_refuses_when_drive_not_present(tmp_path) -> None:
    """No device_basenames entry for the serial → refuse with a clear
    flash banner explaining 're-insert to regrade'."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    _seed_drive_and_run(state, "GHOST-SERIAL")

    # Explicitly DON'T populate state.device_basenames — drive is absent.
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/drives/GHOST-SERIAL/regrade")
    assert resp.status_code == 303
    assert "regrade_error=" in resp.headers["location"]
    assert "not+currently+plugged" in resp.headers["location"] or "not%20currently%20plugged" in resp.headers["location"]


def test_regrade_refuses_when_pipeline_active(tmp_path) -> None:
    """Can't regrade a drive with a running pipeline — the SMART read
    would interleave with the pipeline's own smartctl calls on the
    same device, and we'd stomp state anyway."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    _seed_drive_and_run(state, "BUSY-SERIAL")
    state.device_basenames["BUSY-SERIAL"] = "sdz"
    state.active_phase["BUSY-SERIAL"] = "badblocks"

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/drives/BUSY-SERIAL/regrade")
    assert resp.status_code == 303
    assert "regrade_error=" in resp.headers["location"]
    assert "running+a+pipeline" in resp.headers["location"] or "running%20a%20pipeline" in resp.headers["location"]


def test_regrade_refuses_when_no_prior_abc_run(tmp_path) -> None:
    """Drive exists + is present + is idle, but has only F or NULL
    grades → nothing to regrade from."""
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m

    app = _bootstrap_app(tmp_path)
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="FAILED-ONLY",
            model="Test Drive",
            capacity_bytes=1_000_000_000_000,
            transport="sata",
        ))
        # Only an F run, not A/B/C
        session.add(m.TestRun(
            drive_serial="FAILED-ONLY",
            phase="done",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            grade="F",
        ))
        session.commit()
    state.device_basenames["FAILED-ONLY"] = "sdz"

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/drives/FAILED-ONLY/regrade")
    assert resp.status_code == 303
    assert "regrade_error=" in resp.headers["location"]
    assert "no+prior" in resp.headers["location"] or "no%20prior" in resp.headers["location"]


def test_regrade_success_creates_new_testrun_with_link(tmp_path) -> None:
    """Happy path: fresh SMART pulled, new TestRun(phase='regrade')
    persisted, regrade_of_run_id points at the source."""
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m
    from driveforge.core.smart import SmartSnapshot

    app = _bootstrap_app(tmp_path)
    state = get_state()
    source_run_id = _seed_drive_and_run(state, "IDLE-SERIAL", grade="A")
    state.device_basenames["IDLE-SERIAL"] = "sdz"

    # Stub smartctl read — return a snapshot that shows the drive has
    # aged past the A ceiling so we can verify grade re-computes.
    fake_snap = SmartSnapshot(
        device="/dev/sdz",
        captured_at=datetime.now(UTC),
        power_on_hours=40000,  # > 35040 A ceiling → should regrade to B
        reallocated_sectors=0,
        current_pending_sector=0,
        offline_uncorrectable=0,
        smart_status_passed=True,
    )

    async def fake_snapshot_async(device, **kw):
        return fake_snap

    with patch("driveforge.core.smart.snapshot_async", new=AsyncMock(side_effect=fake_snapshot_async)), \
         patch("driveforge.core.printer.auto_print_cert_for_run", return_value=(True, "ok")):
        with TestClient(app, follow_redirects=False) as client:
            resp = client.post("/drives/IDLE-SERIAL/regrade")

    assert resp.status_code == 303
    assert "regrade_ok=" in resp.headers["location"]

    # New TestRun exists with phase=regrade and regrade_of_run_id set
    with state.session_factory() as session:
        runs = (
            session.query(m.TestRun)
            .filter_by(drive_serial="IDLE-SERIAL")
            .order_by(m.TestRun.id.desc())
            .all()
        )
        assert len(runs) == 2
        new_run, source = runs[0], runs[1]
        assert new_run.phase == "regrade"
        assert new_run.regrade_of_run_id == source_run_id
        assert new_run.grade == "B"  # age ceiling demoted from A
        # Source preserved unchanged
        assert source.grade == "A"


def test_regrade_preserves_source_pipeline_fields(tmp_path) -> None:
    """Regrade run must copy forward throughput + sanitization_method
    from the source — we're not re-running badblocks, so the
    transparency report still needs those fields populated."""
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m
    from driveforge.core.smart import SmartSnapshot

    app = _bootstrap_app(tmp_path)
    state = get_state()
    # Seed a drive + run with non-default throughput stats
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="PRESERVE-SERIAL",
            model="Test Drive",
            capacity_bytes=1_000_000_000_000,
            transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="PRESERVE-SERIAL",
            phase="done",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            grade="A",
            reallocated_sectors=0,
            power_on_hours_at_test=1000,
            throughput_mean_mbps=175.5,
            throughput_p5_mbps=160.0,
            throughput_p95_mbps=190.0,
            throughput_pass_means=[170.0, 175.0, 176.0, 175.0, 175.5, 175.0, 175.0, 175.5],
            sanitization_method="secure_erase",
        ))
        session.commit()
    state.device_basenames["PRESERVE-SERIAL"] = "sdz"

    async def fake_snapshot_async(device, **kw):
        return SmartSnapshot(
            device="/dev/sdz",
            captured_at=datetime.now(UTC),
            power_on_hours=1500,  # still A-eligible
            reallocated_sectors=0,
            current_pending_sector=0,
            offline_uncorrectable=0,
            smart_status_passed=True,
        )

    with patch("driveforge.core.smart.snapshot_async", new=AsyncMock(side_effect=fake_snapshot_async)), \
         patch("driveforge.core.printer.auto_print_cert_for_run", return_value=(True, "ok")):
        with TestClient(app, follow_redirects=False) as client:
            client.post("/drives/PRESERVE-SERIAL/regrade")

    with state.session_factory() as session:
        new_run = (
            session.query(m.TestRun)
            .filter_by(drive_serial="PRESERVE-SERIAL", phase="regrade")
            .first()
        )
        assert new_run is not None
        # Throughput + sanitization_method preserved from source
        assert new_run.throughput_mean_mbps == 175.5
        assert new_run.sanitization_method == "secure_erase"
        assert new_run.throughput_pass_means is not None
        assert len(new_run.throughput_pass_means) == 8
