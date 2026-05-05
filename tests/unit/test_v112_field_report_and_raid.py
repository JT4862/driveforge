"""v1.1.2 — terminal field-check report + RAID-controller detection.

Shipped to fix two operator-visible gaps in the v1.1.1 Live ISO:

  1. No way to see the report without a network. Operator with the
     server's keyboard + monitor but no laptop / no LAN couldn't
     get at the web UI on localhost:8080.

     Fix: a `driveforge-field-report` CLI command that renders the
     same data the web UI shows, but to stdout with ANSI colors.
     The Live ISO autologs in on tty1 and runs this on first
     login via /root/.bash_profile.

  2. Servers with RAID controllers (PERC, MegaRAID, Smart Array)
     in non-IT mode show garbage data. lsblk reports virtual disks,
     smartctl returns "no SMART support" or controller-bus health
     instead of per-drive SMART. The pre-v1.1.2 report would just
     show empty grades with no explanation.

     Fix: detect RAID-mode controllers via lspci, detect virtual-
     disk model strings via lsblk, surface a clear warning + skip
     SMART probing on virtual disks.

Tests:
  - is_raid_controller_description: positive (PERC, MegaRAID, etc.)
    + negative (Fusion-MPT, SAS3008 passthrough)
  - is_raid_virtual_disk_model: positive (PERC V, AVAGO MR) +
    negative (real drive models like ST3000NM0033)
  - detect_raid_situation: empty case, raid-only, mixed
    raid+passthrough
  - field_report.render: produces non-empty output, contains
    headline, contains drive table headers
  - field_report renders RAID warning when storage_controllers
    includes a RAID match
  - field_report handles all-virtual-disks gracefully (no SMART
    probe attempted)
"""

from __future__ import annotations

from unittest.mock import patch

from driveforge.core import server_info
from driveforge import field_report


# ============================================================ RAID detection


def test_is_raid_controller_description_positive() -> None:
    """Common RAID controller descriptions match."""
    samples = [
        "RAID bus controller: LSI Logic / Symbios Logic MegaRAID SAS-3 3108",
        "Hewlett-Packard Company Smart Array Gen8 Controllers",
        "Dell PERC H710 Mini (Embedded)",
        "Adaptec AAC-RAID",
        "RAID bus controller: 3ware Inc 9650SE SATA-II RAID",
    ]
    for s in samples:
        assert server_info.is_raid_controller_description(s), s


def test_is_raid_controller_description_negative() -> None:
    """IT-mode HBAs + standard SATA controllers do NOT match."""
    samples = [
        "Serial Attached SCSI controller: LSI Logic / Symbios Logic SAS2308 PCI-Express Fusion-MPT SAS-2",
        "Serial Attached SCSI controller: Broadcom / LSI SAS3008 PCI-Express Fusion-MPT SAS-3",
        "SATA controller: Intel Corporation C610/X99 series chipset 6-Port SATA Controller",
        "Non-Volatile memory controller: Samsung NVMe SSD Controller",
        "",
    ]
    for s in samples:
        assert not server_info.is_raid_controller_description(s), s


def test_is_raid_virtual_disk_model_positive() -> None:
    """Common RAID virtual-disk model strings match."""
    samples = [
        "PERC H710 V",
        "AVAGO MR9361-8i",
        "MegaRAID 9271-8i",
        "Smart Array P420i",
        "LSILogic Logical Volume",
    ]
    for s in samples:
        assert server_info.is_raid_virtual_disk_model(s), s


def test_is_raid_virtual_disk_model_negative() -> None:
    """Real drive models do NOT match — they're real drives."""
    samples = [
        "ST3000NM0033",
        "INTEL SSDSC2BB120G4",
        "WDC WD1000CHTZ",
        "Samsung SSD 970 EVO Plus 1TB",
        "HGST HUS726T6TALE6L4",
        "",
    ]
    for s in samples:
        assert not server_info.is_raid_virtual_disk_model(s), s


def test_detect_raid_situation_clean_passthrough_only() -> None:
    """Server with only an IT-mode HBA → no RAID warning."""
    info = server_info.ServerInfo(
        storage_controllers=[
            "Serial Attached SCSI controller: LSI Logic / Symbios Logic SAS2308 PCI-Express Fusion-MPT SAS-2",
        ],
    )
    out = server_info.detect_raid_situation(info)
    assert out["has_raid_controller"] is False
    assert out["raid_controllers"] == []
    assert out["has_passthrough_hba"] is True


def test_detect_raid_situation_raid_only() -> None:
    info = server_info.ServerInfo(
        storage_controllers=["Dell PERC H710 Mini (Embedded)"],
    )
    out = server_info.detect_raid_situation(info)
    assert out["has_raid_controller"] is True
    assert "PERC" in out["raid_controllers"][0]
    assert out["has_passthrough_hba"] is False


def test_detect_raid_situation_mixed() -> None:
    """Some servers ship with BOTH a RAID card (front bays) AND a
    separate HBA (rear bays). The mixed flag tells the report to
    say 'partial visibility'."""
    info = server_info.ServerInfo(
        storage_controllers=[
            "RAID bus controller: LSI MegaRAID SAS-3 3108",
            "Serial Attached SCSI controller: Broadcom / LSI SAS3008 PCI-Express Fusion-MPT SAS-3",
        ],
    )
    out = server_info.detect_raid_situation(info)
    assert out["has_raid_controller"] is True
    assert out["has_passthrough_hba"] is True


# ============================================================ Field report rendering


def test_render_produces_non_empty_output(monkeypatch) -> None:
    """End-to-end render returns a non-empty string with the
    DriveForge headline."""
    monkeypatch.setattr(server_info, "collect", lambda: server_info.ServerInfo(
        manufacturer="Dell Inc.", product_name="PowerEdge R720",
        cpu_model="Intel(R) Xeon(R) CPU E5-2680 v2",
    ))
    from driveforge.core import drive as drive_mod
    monkeypatch.setattr(drive_mod, "discover", lambda: [])
    out = field_report.render()
    assert "DriveForge Field-Check" in out
    assert "PowerEdge R720" in out
    # No drives → renders the empty-state message
    assert "Drives (0" in out


def test_render_includes_raid_warning_when_present(monkeypatch) -> None:
    """RAID controller in storage_controllers → warning section in output."""
    monkeypatch.setattr(server_info, "collect", lambda: server_info.ServerInfo(
        manufacturer="Dell Inc.", product_name="PowerEdge R720",
        storage_controllers=["Dell PERC H710 Mini (Embedded)"],
    ))
    from driveforge.core import drive as drive_mod
    monkeypatch.setattr(drive_mod, "discover", lambda: [])
    out = field_report.render()
    assert "RAID controller detected" in out
    assert "PERC" in out
    # The actionable next-step block is present
    assert "crossflashable" in out or "HBA / IT-mode" in out


def test_render_no_raid_warning_on_clean_passthrough(monkeypatch) -> None:
    """Pure IT-mode HBA → no RAID warning text."""
    monkeypatch.setattr(server_info, "collect", lambda: server_info.ServerInfo(
        manufacturer="Dell Inc.", product_name="PowerEdge R720",
        storage_controllers=[
            "Serial Attached SCSI controller: LSI Logic SAS2308 Fusion-MPT SAS-2",
        ],
    ))
    from driveforge.core import drive as drive_mod
    monkeypatch.setattr(drive_mod, "discover", lambda: [])
    out = field_report.render()
    assert "RAID controller detected" not in out


def test_render_skips_smart_probe_on_virtual_disks(monkeypatch) -> None:
    """A drive whose model matches a RAID virtual-disk pattern is
    listed but NOT SMART-probed (the row says '(RAID volume)' instead
    of a grade). Verifies by raising in the SMART probe — if the
    code path is correct, the test passes because smart_mod.snapshot
    never gets called for the virtual disk."""
    from driveforge.core import drive as drive_mod
    from driveforge.core.drive import Drive, Transport
    from driveforge.core import smart as smart_mod

    monkeypatch.setattr(server_info, "collect", lambda: server_info.ServerInfo())
    fake_drives = [
        Drive(
            serial="VIRTUAL-1", model="PERC H710 V",
            capacity_bytes=8_000_000_000_000,
            device_path="/dev/sda", transport=Transport.SAS,
        ),
    ]
    monkeypatch.setattr(drive_mod, "discover", lambda: fake_drives)

    smart_called = []

    def boom(*args, **kwargs):
        smart_called.append(args)
        raise RuntimeError("smartctl should NOT be called for a virtual disk")

    monkeypatch.setattr(smart_mod, "snapshot", boom)

    out = field_report.render()
    assert smart_called == [], "field-report tried to SMART-probe a RAID virtual disk"
    assert "VIRTUAL-1" in out
    assert "RAID volume" in out


def test_render_handles_smart_probe_failure_gracefully(monkeypatch) -> None:
    """If smartctl errors on a real drive, the row shows ERR + the
    error text — but the report itself still renders successfully."""
    from driveforge.core import drive as drive_mod
    from driveforge.core.drive import Drive, Transport
    from driveforge.core import smart as smart_mod

    monkeypatch.setattr(server_info, "collect", lambda: server_info.ServerInfo())
    monkeypatch.setattr(drive_mod, "discover", lambda: [
        Drive(
            serial="REAL-1", model="ST3000NM0033",
            capacity_bytes=3_000_000_000_000,
            device_path="/dev/sdb", transport=Transport.SATA,
            rotation_rate=7200,
        ),
    ])
    monkeypatch.setattr(
        smart_mod, "snapshot",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated smartctl failure")),
    )
    out = field_report.render()
    assert "REAL-1" in out
    assert "ERR" in out
    assert "simulated smartctl failure" in out


def test_render_includes_air_gap_message_when_no_network(monkeypatch) -> None:
    """When _detect_daemon_url returns None (no IP), the footer
    explains the daemon is air-gapped."""
    monkeypatch.setattr(server_info, "collect", lambda: server_info.ServerInfo())
    from driveforge.core import drive as drive_mod
    monkeypatch.setattr(drive_mod, "discover", lambda: [])
    monkeypatch.setattr(field_report, "_detect_daemon_url", lambda: None)
    out = field_report.render()
    assert "air-gapped" in out


def test_main_returns_zero_on_success(monkeypatch, capsys) -> None:
    """Exit code 0 means the operator's shell prompt comes back
    cleanly. Exit code 1 means we want them to see something went
    wrong (top-level render failure only)."""
    monkeypatch.setattr(server_info, "collect", lambda: server_info.ServerInfo())
    from driveforge.core import drive as drive_mod
    monkeypatch.setattr(drive_mod, "discover", lambda: [])
    rc = field_report.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "DriveForge Field-Check" in out


def test_main_returns_one_on_top_level_render_failure(monkeypatch) -> None:
    """If the render itself crashes (not just per-drive failure),
    main exits 1 so the wrapping script knows to bail."""
    monkeypatch.setattr(field_report, "render", lambda: (_ for _ in ()).throw(
        RuntimeError("synthetic top-level render failure"),
    ))
    rc = field_report.main()
    assert rc == 1
