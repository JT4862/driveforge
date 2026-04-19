from __future__ import annotations

from driveforge.core import drive


def test_discover_returns_expected_drive_count() -> None:
    drives = drive.discover()
    assert len(drives) == 5


def test_discover_populates_core_fields() -> None:
    drives = {d.serial: d for d in drive.discover()}
    hgst = drives["V8G6X4RL"]
    assert hgst.model == "HGST HUS726T6TALE6L4"
    assert hgst.capacity_tb == 6.0
    assert hgst.transport == drive.Transport.SATA
    assert hgst.device_path == "/dev/sda"


def test_discover_classifies_nvme() -> None:
    drives = {d.serial: d for d in drive.discover()}
    nvme = drives["S5GXNG0N101923L"]
    assert nvme.transport == drive.Transport.NVME
    assert nvme.is_ssd
    assert nvme.device_path == "/dev/nvme0n1"


def test_drive_flags_ssds_correctly() -> None:
    drives = {d.serial: d for d in drive.discover()}
    intel = drives["BTYG91230A9N960CGN"]
    assert intel.transport == drive.Transport.SATA
    assert intel.is_ssd
