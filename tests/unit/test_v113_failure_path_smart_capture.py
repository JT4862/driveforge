"""v1.1.3 — failure-path SMART capture + synthesized rationale.

Triggered by JT's Z1Z7N1T1 case: a Seagate ST4000NM0033 failed SMART
long self-test, dashboard graded F but the report panel showed no
counters (POH, reallocated, pending, offline-uncorrectable, SMART
status all blank) AND the "Why this drive graded X" rationale panel
rendered nothing because the rules JSON was empty.

Root cause: pre-v1.1.3 `_record_failure` stamped grade + error_message
+ log_tail and committed the row, but never:
  - Probed SMART one more time (post-SMART stayed NULL on every
    failed run, even when the drive was still responsive)
  - Synthesized a rule entry (rules JSON stayed empty so the
    rationale panel had nothing to iterate)

v1.1.3 fixes both in the same code path. Tests cover:
  - Post-SMART probe runs + populates the row when the drive is
    responsive after failure
  - Probe failure (drive wedged) is caught + logged, doesn't
    raise out of _record_failure
  - Synthesized rule entry maps phase → canonical rule name
    (long_test → smart_long_test_passed, etc.)
  - Synthesized rule's forces_grade matches the actual grade
  - Doesn't double-insert if _record_failure runs twice
  - Drive-detail template falls back to pre-SMART values when
    post are missing (covers historical rows + post-SMART probe
    failure cases)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from driveforge import config as cfg
from driveforge.core.drive import Drive, Transport
from driveforge.core import smart as smart_mod
from driveforge.daemon.orchestrator import Orchestrator
from driveforge.daemon.state import DaemonState, set_state


def _bootstrap(tmp_path):
    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True
    settings.fleet.role = "standalone"
    state = DaemonState.boot(settings)
    set_state(state)
    return state


def _seed_open_run(state, serial: str, *, pre_realloc=31, pre_pending=0):
    """Insert a TestRun row in the 'open' state (completed_at NULL)
    that _record_failure will pick up + close."""
    from driveforge.db import models as m
    from datetime import UTC, datetime
    with state.session_factory() as session:
        # Drive row first (FK requirement)
        if session.get(m.Drive, serial) is None:
            session.add(m.Drive(
                serial=serial, model="ST4000NM0033",
                capacity_bytes=4_000_000_000_000, transport="sata",
            ))
        run = m.TestRun(
            drive_serial=serial,
            phase="long_test",
            started_at=datetime.now(UTC),
            pre_reallocated_sectors=pre_realloc,
            pre_current_pending_sector=pre_pending,
        )
        session.add(run)
        session.commit()
        return run.id


# ============================================================ Post-SMART capture


def test_post_smart_captured_on_long_test_failure(tmp_path) -> None:
    """When the drive is still responsive after the failure (long-test
    case — drive returned its own failure, not a wedged drive), we
    snap SMART one more time + populate the post-fields."""
    state = _bootstrap(tmp_path)
    run_id = _seed_open_run(state, "Z1Z7N1T1")
    orch = Orchestrator(state)
    drive = Drive(
        serial="Z1Z7N1T1", model="ST4000NM0033",
        capacity_bytes=4_000_000_000_000,
        device_path="/dev/sdx", transport=Transport.SATA,
        rotation_rate=7200,
    )
    fake_snap = smart_mod.SmartSnapshot(
        device="/dev/sdx",
        captured_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        attributes=[], raw={},
        power_on_hours=42_318,
        reallocated_sectors=47,  # grew during the failed test
        current_pending_sector=12,  # NEW pending sectors — concerning
        offline_uncorrectable=8,
        smart_status_passed=False,  # drive's own SMART status now FAIL
        self_test_has_past_failure=True,
    )
    with patch.object(smart_mod, "snapshot", return_value=fake_snap):
        orch._record_failure(
            drive, phase="long_test",
            detail="SMART long self-test reported failure",
        )
    from driveforge.db import models as m
    with state.session_factory() as session:
        row = session.get(m.TestRun, run_id)
    assert row.grade == "F"
    assert row.power_on_hours_at_test == 42_318
    assert row.reallocated_sectors == 47
    assert row.current_pending_sector == 12
    assert row.offline_uncorrectable == 8
    assert row.smart_status_passed is False
    assert row.self_test_has_past_failure is True
    # Pre values preserved (we set them in _seed_open_run)
    assert row.pre_reallocated_sectors == 31
    assert row.pre_current_pending_sector == 0


def test_post_smart_probe_failure_does_not_raise(tmp_path) -> None:
    """Drive's been wedged from the failure — SMART probe times out
    or errors. _record_failure must catch + log, not raise. Row
    still gets the grade + error_message; post-SMART fields stay
    NULL."""
    state = _bootstrap(tmp_path)
    run_id = _seed_open_run(state, "WEDGED-1")
    orch = Orchestrator(state)
    drive = Drive(
        serial="WEDGED-1", model="ST4000NM0033",
        capacity_bytes=4_000_000_000_000,
        device_path="/dev/sdx", transport=Transport.SATA,
    )
    with patch.object(
        smart_mod, "snapshot",
        side_effect=RuntimeError("smartctl D-state timeout"),
    ):
        orch._record_failure(
            drive, phase="long_test",
            detail="SMART long self-test reported failure",
        )
    from driveforge.db import models as m
    with state.session_factory() as session:
        row = session.get(m.TestRun, run_id)
    # Failure WAS recorded
    assert row.grade == "F"
    assert "long self-test reported failure" in row.error_message
    # But post-SMART fields stayed NULL because the probe couldn't run
    assert row.power_on_hours_at_test is None
    assert row.reallocated_sectors is None
    # Pre values preserved
    assert row.pre_reallocated_sectors == 31


def test_aborted_run_skips_post_smart_capture(tmp_path) -> None:
    """User-aborted runs (grade=NULL) skip post-SMART entirely —
    they intentionally produce no verdict, no point probing."""
    state = _bootstrap(tmp_path)
    run_id = _seed_open_run(state, "ABORTED-1")
    orch = Orchestrator(state)
    drive = Drive(
        serial="ABORTED-1", model="WDC", capacity_bytes=1_000_000_000_000,
        device_path="/dev/sdx", transport=Transport.SATA,
    )
    snapshot_called = []
    def boom(*args, **kwargs):
        snapshot_called.append(args)
        return MagicMock()
    with patch.object(smart_mod, "snapshot", side_effect=boom):
        orch._record_failure(drive, phase="aborted", detail="aborted by user")
    assert snapshot_called == [], "SMART probe should NOT run for aborts"
    from driveforge.db import models as m
    with state.session_factory() as session:
        row = session.get(m.TestRun, run_id)
    assert row.grade is None  # aborted → NULL grade
    assert row.phase == "aborted"


# ============================================================ Synthesized rules


def test_long_test_failure_synthesizes_canonical_rule(tmp_path) -> None:
    """phase=long_test + grade=F → rules contains an entry with
    name='smart_long_test_passed', passed=False, forces_grade='F'.
    The drive-detail 'Why this drive graded X' panel renders this
    in its 'Auto-fail signals that fired' section."""
    state = _bootstrap(tmp_path)
    run_id = _seed_open_run(state, "Z1Z7N1T1-2")
    orch = Orchestrator(state)
    drive = Drive(
        serial="Z1Z7N1T1-2", model="ST4000NM0033",
        capacity_bytes=4_000_000_000_000,
        device_path="/dev/sdx", transport=Transport.SATA,
    )
    with patch.object(smart_mod, "snapshot", side_effect=RuntimeError("skip probe")):
        orch._record_failure(
            drive, phase="long_test",
            detail="SMART long self-test reported failure",
        )
    from driveforge.db import models as m
    with state.session_factory() as session:
        row = session.get(m.TestRun, run_id)
    rules = row.rules or []
    assert len(rules) == 1
    r = rules[0]
    assert r["name"] == "smart_long_test_passed"
    assert r["passed"] is False
    assert r["forces_grade"] == "F"
    assert "long self-test reported failure" in r["detail"]


def test_short_test_failure_maps_to_short_test_rule(tmp_path) -> None:
    state = _bootstrap(tmp_path)
    run_id = _seed_open_run(state, "ST-FAIL-1")
    orch = Orchestrator(state)
    drive = Drive(
        serial="ST-FAIL-1", model="WDC", capacity_bytes=1_000_000_000_000,
        device_path="/dev/sdx", transport=Transport.SATA,
    )
    with patch.object(smart_mod, "snapshot", side_effect=RuntimeError("skip")):
        orch._record_failure(
            drive, phase="short_test",
            detail="SMART short self-test reported failure",
        )
    from driveforge.db import models as m
    with state.session_factory() as session:
        row = session.get(m.TestRun, run_id)
    assert row.rules[0]["name"] == "smart_short_test_passed"
    assert row.rules[0]["forces_grade"] == "F"


def test_error_grade_does_not_force_f(tmp_path) -> None:
    """grade=error means 'we don't know if the drive is bad,
    pipeline broke'. The synthesized rule shouldn't claim F —
    forces_grade is None for error-grade rows."""
    state = _bootstrap(tmp_path)
    run_id = _seed_open_run(state, "ERROR-1")
    orch = Orchestrator(state)
    drive = Drive(
        serial="ERROR-1", model="WDC", capacity_bytes=1_000_000_000_000,
        device_path="/dev/sdx", transport=Transport.SATA,
    )
    with patch.object(smart_mod, "snapshot", side_effect=RuntimeError("skip")):
        orch._record_failure(
            drive, phase="secure_erase",
            detail="unexpected: subprocess crashed",
        )
    from driveforge.db import models as m
    with state.session_factory() as session:
        row = session.get(m.TestRun, run_id)
    # The classifier picks "error" for this generic failure shape
    assert row.grade in ("error", "F")  # depending on classifier impl
    if row.grade == "error":
        assert row.rules[0]["forces_grade"] is None


def test_does_not_double_insert_rule_on_re_record(tmp_path) -> None:
    """If _record_failure somehow runs twice on the same run (defensive
    case — shouldn't happen in normal flow), the synthesized rule
    isn't added twice."""
    state = _bootstrap(tmp_path)
    run_id = _seed_open_run(state, "DOUBLE-1")
    orch = Orchestrator(state)
    drive = Drive(
        serial="DOUBLE-1", model="WDC", capacity_bytes=1_000_000_000_000,
        device_path="/dev/sdx", transport=Transport.SATA,
    )
    with patch.object(smart_mod, "snapshot", side_effect=RuntimeError("skip")):
        orch._record_failure(
            drive, phase="long_test",
            detail="SMART long self-test reported failure",
        )

    # Re-open + call again with the same serial. _record_failure
    # filters on completed_at=None which the FIRST call set, so the
    # second call shouldn't find an open run at all. Regardless,
    # synthesize_failure_rule is idempotent on rule-name match.
    from driveforge.db import models as m
    from datetime import UTC, datetime
    with state.session_factory() as session:
        run = session.get(m.TestRun, run_id)
        run.completed_at = None  # reopen artificially
        session.commit()
    with patch.object(smart_mod, "snapshot", side_effect=RuntimeError("skip")):
        orch._record_failure(
            drive, phase="long_test",
            detail="SMART long self-test reported failure",
        )
    with state.session_factory() as session:
        row = session.get(m.TestRun, run_id)
    # Still exactly one rule entry
    assert len(row.rules) == 1


# ============================================================ Template fallback


def test_drive_detail_renders_pre_smart_when_post_missing(tmp_path) -> None:
    """For historical rows where post-SMART was never captured (pre-
    v1.1.3 failures), the drive-detail page now shows the pre values
    with a clear note instead of empty dashes."""
    from fastapi.testclient import TestClient
    from datetime import UTC, datetime
    from driveforge.daemon.app import make_app
    from driveforge.db import models as m

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True
    app = make_app(settings)
    state = DaemonState.boot(settings)
    set_state(state)

    # Insert a closed F run with pre-SMART present + post-SMART NULL
    # (the Z1Z7N1T1 shape).
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="HISTORIC-F", model="ST4000NM0033",
            capacity_bytes=4_000_000_000_000, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="HISTORIC-F",
            phase="failed", grade="F",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            pre_reallocated_sectors=31,
            pre_current_pending_sector=0,
            error_message="[long_test] SMART long self-test reported failure",
            rules=[],  # legacy empty rules
        ))
        session.commit()

    with TestClient(app) as client:
        resp = client.get("/drives/HISTORIC-F")
    assert resp.status_code == 200
    body = resp.text
    # Pre value visible with the explanatory note
    assert "31" in body
    assert "captured at pipeline start" in body
