from __future__ import annotations

from pathlib import Path

from driveforge.core import firmware
from driveforge.core.drive import Drive, Transport


DB_PATH = Path(__file__).parent.parent.parent / "driveforge" / "data" / "firmware_db.yaml"


def _drive(*, model: str, fw: str | None, transport: Transport = Transport.SATA) -> Drive:
    return Drive(
        serial="TEST",
        model=model,
        capacity_bytes=1_000_000_000,
        transport=transport,
        device_path="/dev/sda",
        firmware_version=fw,
    )


def test_oem_branded_drive_is_skipped() -> None:
    drive = _drive(model="DELL-INTERNAL-MODEL", fw="ABCD")
    check = firmware.check_firmware(drive, db_path=DB_PATH)
    assert check.update_available is False
    assert "OEM" in check.reason


def test_unknown_model_reports_no_entry() -> None:
    drive = _drive(model="SomeRandomModel", fw="0001")
    check = firmware.check_firmware(drive, db_path=DB_PATH)
    assert check.update_available is False
    assert "no DB entry" in check.reason


def test_matching_latest_version_reports_up_to_date() -> None:
    drive = _drive(model="HGST HUS726T6TALE6L4", fw="K8GNW7LH")
    check = firmware.check_firmware(drive, db_path=DB_PATH)
    assert check.update_available is False
    assert check.latest_version == "K8GNW7LH"
