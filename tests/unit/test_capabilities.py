"""Tests for hardware-capability detection."""

from __future__ import annotations

from pathlib import Path

from driveforge.core import capabilities, enclosures


def test_capabilities_reflect_ses_led_and_ipmi_fixtures(tmp_path: Path) -> None:
    # Synthetic SES enclosure so led_control picks up.
    enc_root = tmp_path / "sys" / "class" / "enclosure" / "0:0:32:0"
    (enc_root / "device" / "scsi_generic" / "sg3").mkdir(parents=True)
    (enc_root / "device" / "vendor").write_text("DELL")
    (enc_root / "device" / "model").write_text("PERC")
    (enc_root / "id").write_text("0xabc")
    slot = enc_root / "Slot_00"
    slot.mkdir()
    (slot / "slot").write_text("0")
    (slot / "status").write_text("OK")
    (slot / "type").write_text("23")

    plan = enclosures.build_bay_plan(sys_root=tmp_path)
    caps = capabilities.detect(plan=plan)
    # With an SES sg_device present, LED control is available
    assert caps.led_control is True
    # The fixture runner has ipmitool stdouts for dcmi and sdr → both pass
    assert caps.chassis_power is True
    assert caps.chassis_temperature is True
    assert caps.any_bmc_feature is True


def test_capabilities_on_empty_host(tmp_path: Path) -> None:
    # No SES enclosure tree at all → led_control is False.
    # ipmitool fixtures still succeed in the test runner, so BMC booleans
    # stay True — this test isolates the enclosure side.
    plan = enclosures.build_bay_plan(sys_root=tmp_path)
    caps = capabilities.detect(plan=plan)
    assert caps.led_control is False
