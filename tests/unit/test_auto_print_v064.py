"""Tests for v0.6.4's auto-print-at-pipeline-completion feature.

Pre-v0.6.4, the cert label only printed when the operator clicked the
"Print Label" button on the drive detail page. That was friction for
any multi-drive batch — the whole point of a refurbishment pipeline
is automation end-to-end.

v0.6.4 calls `auto_print_cert_for_run` from `orchestrator._finalize_run`
when the drive has a grade and `printer.auto_print` is enabled. Print
failures are logged but do NOT fail the run — the drive's grade
stands, operator can click Print Label manually to retry.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from driveforge.core.printer import (
    auto_print_cert_for_run,
    build_cert_label_data_from_run,
)


def _mock_drive(serial="TEST-SN", model="TEST DRIVE"):
    """Minimal Drive stand-in for the printer path."""
    return SimpleNamespace(
        serial=serial,
        model=model,
        capacity_bytes=1_000_000_000_000,
    )


def _mock_run(grade="A", rules=None, quick_mode=False):
    """Minimal TestRun stand-in covering the fields the label builder reads."""
    return SimpleNamespace(
        grade=grade,
        quick_mode=quick_mode,
        completed_at=None,
        started_at=None,
        power_on_hours_at_test=12345,
        reallocated_sectors=0,
        current_pending_sector=0,
        pre_reallocated_sectors=0,
        pre_current_pending_sector=0,
        offline_uncorrectable=0,
        smart_status_passed=True,
        rules=rules or [],
        throughput_mean_mbps=180.0,
    )


def _mock_state(
    *,
    printer_model="QL-820NWB",
    auto_print=True,
    tunnel_hostname=None,
    daemon_host="127.0.0.1",
    daemon_port=8080,
):
    """Minimal state stand-in with printer + integrations + daemon settings."""
    printer = SimpleNamespace(
        model=printer_model,
        connection="usb",
        label_roll="DK-1209",
        backend_identifier=None,
        auto_print=auto_print,
    )
    integrations = SimpleNamespace(
        cloudflare_tunnel_hostname=tunnel_hostname,
    )
    daemon = SimpleNamespace(
        host=daemon_host,
        port=daemon_port,
    )
    settings = SimpleNamespace(
        printer=printer,
        integrations=integrations,
        daemon=daemon,
    )
    return SimpleNamespace(settings=settings)


# ───────────────────────── build_cert_label_data_from_run ─────────────────────────

def test_build_cert_label_data_populates_all_fields() -> None:
    """The shared helper must populate every CertLabelData field from
    the TestRun — any miss means the printed label loses information
    (reallocated count, POH, QR URL, etc.)."""
    drive = _mock_drive(serial="SN001", model="INTEL SSDSC2BB120G4")
    run = _mock_run(grade="A")

    data = build_cert_label_data_from_run(
        drive, run, report_url="https://df.example.com/reports/SN001",
    )

    assert data.serial == "SN001"
    assert data.model == "INTEL SSDSC2BB120G4"
    assert data.capacity_tb == 1.0
    assert data.grade == "A"
    assert data.power_on_hours == 12345
    assert data.report_url == "https://df.example.com/reports/SN001"
    assert data.quick_mode is False
    assert data.throughput_mean_mbps == 180.0


def test_build_cert_label_data_parses_badblocks_from_rules() -> None:
    """Badblocks error counts live inside the grading rule `detail`
    string, not directly on TestRun. The label builder must parse
    the 'read=X write=Y compare=Z' format so the sticker shows the
    counts."""
    run = _mock_run(
        grade="C",
        rules=[
            {
                "name": "badblocks_clean",
                "passed": False,
                "detail": "badblocks found errors: read=3 write=1 compare=0",
            },
        ],
    )
    data = build_cert_label_data_from_run(
        _mock_drive(), run, report_url="https://x.example/r/SN",
    )
    assert data.badblocks_errors == (3, 1, 0)


def test_build_cert_label_data_defaults_zero_badblocks_when_pass() -> None:
    """A `badblocks_clean` rule that PASSED (no parseable error
    counts in detail) should yield (0, 0, 0), not None. None would
    render as '—' on the label; we want '0/0/0' for the positive
    assertion."""
    run = _mock_run(
        grade="A",
        rules=[
            {
                "name": "badblocks_clean",
                "passed": True,
                "detail": "badblocks reported no errors",
            },
        ],
    )
    data = build_cert_label_data_from_run(
        _mock_drive(), run, report_url="https://x.example/r/SN",
    )
    assert data.badblocks_errors == (0, 0, 0)


def test_build_cert_label_data_computes_remapped_delta() -> None:
    """v0.5.5+ healing-delta field: post_reallocated - pre_reallocated
    when both are present. Demonstrates how many sectors the drive
    repaired during its burn-in — a pass-tier metric the sticker
    surfaces."""
    run = _mock_run(grade="B")
    run.pre_reallocated_sectors = 5
    run.reallocated_sectors = 12  # drive healed 7 sectors during burn-in
    data = build_cert_label_data_from_run(
        _mock_drive(), run, report_url="https://x.example/r/SN",
    )
    assert data.remapped_during_run == 7


def test_build_cert_label_data_none_remapped_for_legacy_rows() -> None:
    """Pre-v0.5.5 runs have `pre_reallocated_sectors=None` because the
    column didn't exist yet. Must render as None (→ skip the line on
    the label) rather than crash."""
    run = _mock_run(grade="B")
    run.pre_reallocated_sectors = None
    run.reallocated_sectors = 2
    data = build_cert_label_data_from_run(
        _mock_drive(), run, report_url="https://x.example/r/SN",
    )
    assert data.remapped_during_run is None


# ──────────────────────────── auto_print_cert_for_run ────────────────────────────

def test_auto_print_skipped_when_no_printer_configured() -> None:
    """A common case: operator hasn't configured a printer yet.
    auto_print_cert_for_run must return (False, informational message)
    without raising — the pipeline finalize path swallows it
    cleanly."""
    state = _mock_state(printer_model=None)
    drive = _mock_drive()
    run = _mock_run()
    ok, msg = auto_print_cert_for_run(state, drive, run)
    assert ok is False
    assert "no printer" in msg.lower() or "not configured" in msg.lower()


def test_auto_print_skipped_when_toggle_disabled() -> None:
    """Operator has a printer but turned auto-print off in Settings.
    Skip silently — manual Print Label button still works. Return
    False (not attempted) with a 'disabled' message."""
    state = _mock_state(auto_print=False)
    ok, msg = auto_print_cert_for_run(state, _mock_drive(), _mock_run())
    assert ok is False
    assert "disabled" in msg.lower()


def test_auto_print_uses_tunnel_hostname_for_qr_when_configured() -> None:
    """Cloudflare Tunnel hostname → QR code resolves from anywhere.
    Capture the URL that the label data builder gets so we can verify
    it came from the tunnel, not the daemon bind host."""
    captured_url = {}

    def capture_builder(drive, run, *, report_url):
        captured_url["url"] = report_url
        return MagicMock()  # render_label doesn't actually run

    state = _mock_state(
        tunnel_hostname="driveforge.example.com",
        daemon_host="127.0.0.1",
    )
    with patch("driveforge.core.printer.build_cert_label_data_from_run", side_effect=capture_builder), \
         patch("driveforge.core.printer.render_label", return_value=MagicMock()), \
         patch(
             "driveforge.core.printer.print_label",
             return_value=(True, "ok"),
         ):
        auto_print_cert_for_run(state, _mock_drive("SN999"), _mock_run())
    assert captured_url["url"] == "https://driveforge.example.com/reports/SN999"


def test_auto_print_falls_back_to_lan_url_without_tunnel() -> None:
    """No tunnel configured → synthesize from daemon bind host + port.
    LAN-only URL; phones on the same LAN scanning the QR still resolve
    it. Better than no URL or a placeholder."""
    captured_url = {}

    def capture_builder(drive, run, *, report_url):
        captured_url["url"] = report_url
        return MagicMock()

    state = _mock_state(
        tunnel_hostname=None,
        daemon_host="10.10.10.103",
        daemon_port=8080,
    )
    with patch("driveforge.core.printer.build_cert_label_data_from_run", side_effect=capture_builder), \
         patch("driveforge.core.printer.render_label", return_value=MagicMock()), \
         patch(
             "driveforge.core.printer.print_label",
             return_value=(True, "ok"),
         ):
        auto_print_cert_for_run(state, _mock_drive("SN999"), _mock_run())
    assert captured_url["url"] == "http://10.10.10.103:8080/reports/SN999"


def test_auto_print_falls_back_to_mdns_when_bind_is_wildcard() -> None:
    """Bind host is 0.0.0.0 (any interface). That's not a usable URL —
    synthesize `<hostname>.local:<port>` via the gethostname-based
    mDNS path every DriveForge install advertises on."""
    captured_url = {}

    def capture_builder(drive, run, *, report_url):
        captured_url["url"] = report_url
        return MagicMock()

    state = _mock_state(
        tunnel_hostname=None,
        daemon_host="0.0.0.0",
        daemon_port=8080,
    )
    with patch("socket.gethostname", return_value="driveforge-r720"), \
         patch("driveforge.core.printer.build_cert_label_data_from_run", side_effect=capture_builder), \
         patch("driveforge.core.printer.render_label", return_value=MagicMock()), \
         patch(
             "driveforge.core.printer.print_label",
             return_value=(True, "ok"),
         ):
        auto_print_cert_for_run(state, _mock_drive("SN999"), _mock_run())
    assert "driveforge-r720.local" in captured_url["url"]
    assert ":8080/reports/SN999" in captured_url["url"]


def test_auto_print_success_returns_ok_tuple() -> None:
    """Happy path: printer configured, auto_print enabled, render
    succeeds, dispatch succeeds. Returns (True, 'printed ...')."""
    state = _mock_state()
    with patch("driveforge.core.printer.render_label", return_value=MagicMock()), \
         patch(
             "driveforge.core.printer.print_label",
             return_value=(True, "label dispatched to printer"),
         ):
        ok, msg = auto_print_cert_for_run(state, _mock_drive("SN111"), _mock_run())
    assert ok is True
    assert "SN111" in msg


def test_auto_print_surfaces_render_failure() -> None:
    """Render exception must not crash the pipeline finalize path —
    return (False, message) so the orchestrator can log + continue."""
    state = _mock_state()
    with patch(
        "driveforge.core.printer.render_label",
        side_effect=RuntimeError("font file missing"),
    ):
        ok, msg = auto_print_cert_for_run(state, _mock_drive(), _mock_run())
    assert ok is False
    assert "render" in msg.lower()
    assert "font file missing" in msg


def test_auto_print_surfaces_dispatch_failure() -> None:
    """Print dispatch fails (printer offline, wrong roll, etc.) —
    return (False, reason from print_label)."""
    state = _mock_state()
    with patch("driveforge.core.printer.render_label", return_value=MagicMock()), \
         patch(
             "driveforge.core.printer.print_label",
             return_value=(False, "no Brother USB printer detected"),
         ):
        ok, msg = auto_print_cert_for_run(state, _mock_drive(), _mock_run())
    assert ok is False
    assert "no Brother USB printer detected" in msg
