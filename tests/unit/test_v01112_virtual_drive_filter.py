"""v0.11.12 — filter BMC virtual-media devices out of drive discovery.

JT hit this 2026-04-24 when his xVault (Seneca / IPMI BMC) enrolled
as a fleet agent. The operator's dashboard picked up TWO fake
drives alongside the real Seagate 4TB SAS:

  - "Virtual HDisk2" — 0.0 TB, USB transport, serial AAAABBBBCCCC3
  - "Virtual Floppy1" — 0.0 TB, USB transport, serial AAAABBBBCCCC2

These are iDRAC/IPMI virtual-media mounts — the BMC emulates a USB
block device even when no ISO is mounted. JT had already tried
disabling virtual media in BIOS, but that disabled all USB ports,
preventing ISO installs. So we filter at the DriveForge layer.

Two signals v0.11.12 filters on:

  1. size == 0 — a real drive always reports non-zero capacity
     via lsblk, even when fully empty. Zero means "nothing to
     pipeline" regardless of cause.

  2. model.startswith("Virtual ") — vendor convention for BMC-
     emulated devices. Catches the case where a BMC has an ISO
     mounted so size becomes non-zero. DriveForge would otherwise
     try to secure-erase the virtual mount.

Tests:
  - Real drive with non-zero size and regular model: included
  - size=0 drive: filtered
  - model="Virtual HDisk2" with non-zero size: filtered (ISO mounted)
  - model="Virtual Floppy1" with size=0: filtered (both signals)
  - Mixed fixture (real + virtual): only real survives
"""

from __future__ import annotations

import json

from driveforge.core import drive
from driveforge.core.process import ProcessResult


def _mock_lsblk(monkeypatch, entries: list[dict]) -> None:
    """Install a fake `run()` that returns the given lsblk entries."""
    payload = json.dumps({"blockdevices": entries})

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "lsblk":
            return ProcessResult(argv=list(argv), returncode=0, stdout=payload, stderr="")
        # Pass through; tests shouldn't hit other subprocess calls.
        return ProcessResult(argv=list(argv), returncode=1, stdout="", stderr="unmocked")

    # Patch the `run` symbol imported into the drive module (not
    # driveforge.core.process.run itself — drive.py did
    # `from driveforge.core.process import run` so the local binding
    # is what we need to override).
    monkeypatch.setattr(drive, "run", fake_run)
    # Also neutralize root-device detection — our fake lsblk has no
    # mount context, and _root_device_name() shells out to findmnt.
    monkeypatch.setattr(drive, "_root_device_name", lambda: None)


def test_real_drive_included(monkeypatch) -> None:
    _mock_lsblk(monkeypatch, [
        {
            "name": "sda", "model": "ST4000DM004-2CV104",
            "serial": "ZFN4LMJ6", "size": 4_000_787_030_016,
            "tran": "sas", "rota": True, "type": "disk", "rev": "0001",
        },
    ])
    drives = drive.discover()
    assert len(drives) == 1
    assert drives[0].serial == "ZFN4LMJ6"
    assert drives[0].capacity_bytes == 4_000_787_030_016


def test_zero_size_drive_filtered(monkeypatch) -> None:
    """A drive with size=0 (BMC virtual media, no ISO mounted) is
    skipped regardless of its model name."""
    _mock_lsblk(monkeypatch, [
        {
            "name": "sda", "model": "SomeSortOfDrive",
            "serial": "SOMESERIAL", "size": 0,
            "tran": "usb", "rota": False, "type": "disk", "rev": "",
        },
    ])
    drives = drive.discover()
    assert drives == []


def test_virtual_prefix_model_filtered_even_with_size(monkeypatch) -> None:
    """BMC has an ISO mounted so size goes non-zero, but the model
    still starts with 'Virtual '. Refuse to pipeline a
    hypervisor-mounted ISO."""
    _mock_lsblk(monkeypatch, [
        {
            "name": "sdb", "model": "Virtual HDisk2",
            "serial": "AAAABBBBCCCC3",
            "size": 700_000_000,  # ~700 MB — a Debian netinst ISO
            "tran": "usb", "rota": False, "type": "disk", "rev": "",
        },
    ])
    drives = drive.discover()
    assert drives == []


def test_virtual_floppy_filtered(monkeypatch) -> None:
    """The literal device JT saw on xVault."""
    _mock_lsblk(monkeypatch, [
        {
            "name": "sdc", "model": "Virtual Floppy1",
            "serial": "AAAABBBBCCCC2", "size": 0,
            "tran": "usb", "rota": False, "type": "disk", "rev": "",
        },
    ])
    drives = drive.discover()
    assert drives == []


def test_xvault_mixed_fixture_only_real_survives(monkeypatch) -> None:
    """Reproduces JT's exact xVault scenario: one real 4TB Seagate SAS
    drive alongside two virtual-media devices. Only the Seagate
    survives discover()."""
    _mock_lsblk(monkeypatch, [
        {
            "name": "sda", "model": "Virtual HDisk2",
            "serial": "AAAABBBBCCCC3", "size": 0,
            "tran": "usb", "rota": False, "type": "disk", "rev": "",
        },
        {
            "name": "sdb", "model": "ST4000DM004-2CV104",
            "serial": "ZFN4LMJ6", "size": 4_000_787_030_016,
            "tran": "sas", "rota": True, "type": "disk", "rev": "0001",
        },
        {
            "name": "sdc", "model": "Virtual Floppy1",
            "serial": "AAAABBBBCCCC2", "size": 0,
            "tran": "usb", "rota": False, "type": "disk", "rev": "",
        },
    ])
    drives = drive.discover()
    assert len(drives) == 1
    assert drives[0].serial == "ZFN4LMJ6"
    assert drives[0].model == "ST4000DM004-2CV104"


def test_model_name_virtual_without_space_not_filtered(monkeypatch) -> None:
    """Defense-in-depth: the filter matches "Virtual " (with trailing
    space) to avoid false-positives on drives whose model happens to
    start with Virtual (e.g. a hypothetical "Virtualize Pro 1TB").
    Only the literal BMC-convention pattern is filtered."""
    _mock_lsblk(monkeypatch, [
        {
            "name": "sda", "model": "VirtualDriveCo Performance 1TB",
            "serial": "REAL123", "size": 1_000_000_000_000,
            "tran": "sata", "rota": False, "type": "disk", "rev": "0001",
        },
    ])
    drives = drive.discover()
    assert len(drives) == 1
    assert drives[0].serial == "REAL123"
