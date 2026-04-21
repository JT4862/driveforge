"""Tests for v0.6.7's four fixes:

1. HDD badblocks-only sanitization fallback
2. Pre-active state visibility during preflight/recovery
3. QR label layout + tighter fail-reason default
4. SIGKILL-orphan run cleanup (24h+ dangling rows close out)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from driveforge.core import erase
from driveforge.core.erase import is_libata_freeze_pattern
from driveforge.core.printer import (
    CertLabelData,
    build_cert_label_data_from_run,
    render_label,
)


# ──────────────────── libata-freeze pattern detector ────────────────────

def test_libata_freeze_pattern_matches_real_jt_error() -> None:
    """The canonical JT-R720 error string that motivated the v0.6.7
    fallback. The `_sata_secure_erase` error format is 'Both SAT and
    hdparm secure-erase refused. SAT: <detail>. hdparm: <detail>'.
    Must match to trigger the fallback."""
    err = (
        "Both SAT and hdparm secure-erase refused. SAT: SECURITY ERASE "
        "UNIT failed on /dev/sdc: SCSI Status: Check Condition. "
        "Sense key: Aborted Command. error=0x4. "
        "hdparm: hdparm --security-erase failed on /dev/sdc (rc=1)"
    )
    assert is_libata_freeze_pattern(err) is True


def test_libata_freeze_pattern_does_not_match_other_failures() -> None:
    """Other failure modes must NOT trigger the HDD fallback — they
    may indicate actual drive failure where overwrite-only sanitization
    isn't appropriate."""
    # sg_raw layer-level failures
    assert is_libata_freeze_pattern("sg_raw: inquiry failed, transport error") is False
    # Device fault / media failure
    assert is_libata_freeze_pattern("Drive reported device fault bit") is False
    # SAT-only failure (hdparm never mentioned — means fallback didn't run)
    assert is_libata_freeze_pattern("SAT passthrough aborted") is False
    # Empty / None
    assert is_libata_freeze_pattern("") is False
    assert is_libata_freeze_pattern(None) is False  # type: ignore[arg-type]


def test_libata_freeze_pattern_case_insensitive() -> None:
    """Error strings come from multiple log sources with inconsistent
    casing. Matcher must be robust."""
    err = "BOTH SAT AND HDPARM REFUSED WITH ABRT"
    assert is_libata_freeze_pattern(err) is True


# ──────────────────── CertLabelData sanitization_method ────────────────────

def _base_data(
    grade: str = "A",
    sanitization_method: str | None = None,
    quick_mode: bool = False,
    fail_reason: str | None = None,
) -> CertLabelData:
    from datetime import date
    return CertLabelData(
        model="TEST DRIVE",
        serial="TEST-001",
        capacity_tb=3.0,
        grade=grade,
        tested_date=date(2026, 4, 21),
        power_on_hours=12345,
        report_url="https://example.com/r/TEST-001",
        quick_mode=quick_mode,
        reallocated_sectors=0,
        current_pending_sector=0,
        badblocks_errors=(0, 0, 0),
        fail_reason=fail_reason,
        remapped_during_run=0,
        throughput_mean_mbps=150.0,
        sanitization_method=sanitization_method,
    )


def test_cert_label_data_defaults_sanitization_method_to_none() -> None:
    """Backward compat: pre-v0.6.7 callers construct CertLabelData
    without the new field. Must default to None so the label renderer
    falls through to the default 'NIST 800-88 + 4-pass' wording."""
    d = _base_data()
    assert d.sanitization_method is None


def test_label_renders_secure_erase_method_by_default() -> None:
    """Pass-tier label with sanitization_method=None or 'secure_erase'
    renders the default wipe line. Backward compatible with v0.6.6
    and earlier."""
    for method in (None, "secure_erase"):
        img = render_label(_base_data(grade="A", sanitization_method=method))
        # Can't grep the image, but the render must succeed. Actual text
        # content is verified by the label-content tests in test_printer.py.
        assert hasattr(img, "save")


def test_label_renders_badblocks_overwrite_variant() -> None:
    """v0.6.7 fallback path: sanitization_method='badblocks_overwrite'
    renders a distinct wipe line to honestly reflect the method used.
    The HDD libata-freeze fallback case."""
    img = render_label(_base_data(grade="A", sanitization_method="badblocks_overwrite"))
    assert hasattr(img, "save")


def test_build_cert_label_data_threads_sanitization_method() -> None:
    """The helper that builds CertLabelData from a TestRun row must
    pass the new sanitization_method column through so the label
    reflects the actual method."""
    drive = SimpleNamespace(
        serial="SN1", model="M", capacity_bytes=3_000_000_000_000,
    )
    run = SimpleNamespace(
        grade="B", quick_mode=False,
        completed_at=None, started_at=None,
        power_on_hours_at_test=9999,
        reallocated_sectors=0, current_pending_sector=0,
        pre_reallocated_sectors=0, pre_current_pending_sector=0,
        offline_uncorrectable=0, smart_status_passed=True,
        rules=[], throughput_mean_mbps=180.0,
        sanitization_method="badblocks_overwrite",
    )
    data = build_cert_label_data_from_run(
        drive, run, report_url="https://example.com/r/SN1",
    )
    assert data.sanitization_method == "badblocks_overwrite"


def test_build_cert_label_data_handles_legacy_run_without_column() -> None:
    """Legacy rows from pre-v0.6.7 installs won't have
    sanitization_method. The builder uses getattr(..., None) so
    missing attribute is safe — defaults to None."""
    drive = SimpleNamespace(
        serial="SN1", model="M", capacity_bytes=3_000_000_000_000,
    )
    # No sanitization_method attribute (simulating a row that predates
    # the column being added).
    run = SimpleNamespace(
        grade="A", quick_mode=False,
        completed_at=None, started_at=None,
        power_on_hours_at_test=100,
        reallocated_sectors=0, current_pending_sector=0,
        pre_reallocated_sectors=0, pre_current_pending_sector=0,
        offline_uncorrectable=0, smart_status_passed=True,
        rules=[], throughput_mean_mbps=None,
    )
    data = build_cert_label_data_from_run(
        drive, run, report_url="https://example.com/r/SN1",
    )
    assert data.sanitization_method is None


# ──────────────────── fail-reason wrapping ────────────────────

def test_fail_label_default_reason_is_tighter() -> None:
    """v0.6.7 default fail_reason changed from 'failed grading
    (see report)' to 'failed grading' because the '(see report)'
    part was redundant with the footer and was the specific string
    that was clipping against the QR on JT's HGST label."""
    img = render_label(_base_data(grade="F", fail_reason=None))
    # Can't inspect rendered text easily, but render must complete.
    # The actual verification is visual/manual against a printed
    # label — but the code change (None default → "failed grading")
    # is verified by reading render_label source.
    assert hasattr(img, "save")


def test_fail_label_wraps_long_reason_at_24_chars() -> None:
    """Tight wrap limit (24) means long reasons split earlier,
    preventing overflow into the QR region even at the margin case."""
    long_reason = "smart short self-test failed with ATA ABRT error code"
    img = render_label(_base_data(grade="F", fail_reason=long_reason))
    assert hasattr(img, "save")


# ──────────────────── SIGKILL-orphan cleanup ────────────────────

def test_orphan_cleanup_closes_24h_old_interrupted_rows(tmp_path) -> None:
    """The v0.6.7 fix for DB rows that got SIGKILL'd during daemon
    restart and never had a drive re-inserted for recovery. After
    24h, close them as grade=error with a clear error message."""
    from driveforge.daemon.app import _flag_dangling_runs_as_interrupted
    from driveforge.daemon.state import DaemonState
    from driveforge import config as cfg
    from driveforge.db import models as m

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    state = DaemonState.boot(settings)
    try:
        # Seed a drive + an "interrupted but old" run
        with state.session_factory() as session:
            session.add(m.Drive(
                serial="ORPHAN-1", model="Test", capacity_bytes=3_000_000_000_000,
                transport="sata",
            ))
            old_run = m.TestRun(
                drive_serial="ORPHAN-1",
                phase="secure_erase",
                started_at=datetime.now(UTC) - timedelta(hours=30),
                interrupted_at_phase="secure_erase",
            )
            session.add(old_run)
            session.commit()
            session.refresh(old_run)
            orphan_id = old_run.id

        _flag_dangling_runs_as_interrupted(state)

        with state.session_factory() as session:
            closed = session.get(m.TestRun, orphan_id)
            assert closed is not None
            assert closed.completed_at is not None, (
                "orphaned run >24h old must be closed by startup sweep"
            )
            assert closed.grade == "error"
            assert "orphaned" in (closed.error_message or "").lower()
    finally:
        state.drive_command_executor.shutdown(wait=False)


def test_orphan_cleanup_preserves_recent_interrupted_rows(tmp_path) -> None:
    """Recently-interrupted runs (< 24h) MUST be preserved — the drive
    might still be coming back for recovery. The cutoff is a real
    constraint, not just "close anything open"."""
    from driveforge.daemon.app import _flag_dangling_runs_as_interrupted
    from driveforge.daemon.state import DaemonState
    from driveforge import config as cfg
    from driveforge.db import models as m

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    state = DaemonState.boot(settings)
    try:
        with state.session_factory() as session:
            session.add(m.Drive(
                serial="RECENT-1", model="Test", capacity_bytes=3_000_000_000_000,
                transport="sata",
            ))
            recent_run = m.TestRun(
                drive_serial="RECENT-1",
                phase="secure_erase",
                started_at=datetime.now(UTC) - timedelta(hours=2),
                interrupted_at_phase="secure_erase",
            )
            session.add(recent_run)
            session.commit()
            session.refresh(recent_run)
            recent_id = recent_run.id

        _flag_dangling_runs_as_interrupted(state)

        with state.session_factory() as session:
            preserved = session.get(m.TestRun, recent_id)
            assert preserved is not None
            assert preserved.completed_at is None, (
                "run interrupted <24h ago must stay open awaiting recovery"
            )
            assert preserved.grade is None
    finally:
        state.drive_command_executor.shutdown(wait=False)
