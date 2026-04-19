"""Tests for enclosure / bay detection.

Builds synthetic sysfs trees in a tmp_path so tests don't depend on the
host's actual hardware.
"""

from __future__ import annotations

from pathlib import Path

from driveforge.core import enclosures


def _make_enclosure(
    sys_root: Path,
    *,
    enc_name: str,
    vendor: str,
    product: str,
    logical_id: str,
    sg_name: str,
    slots: list[tuple[int, str, str | None]],  # (slot_num, status, device_name)
) -> None:
    """Build one /sys/class/enclosure/<name>/ tree inside sys_root."""
    enc = sys_root / "sys" / "class" / "enclosure" / enc_name
    (enc / "device" / "scsi_generic" / sg_name).mkdir(parents=True)
    (enc / "device" / "vendor").write_text(vendor)
    (enc / "device" / "model").write_text(product)
    (enc / "id").write_text(logical_id)
    for idx, (slot_num, status, device_name) in enumerate(slots):
        slot_dir = enc / f"Slot_{slot_num:02d}"
        slot_dir.mkdir()
        (slot_dir / "slot").write_text(str(slot_num))
        (slot_dir / "status").write_text(status)
        (slot_dir / "type").write_text(str(0x17))  # "Array Device" element type
        if device_name:
            # Create the device dir with a nested block/<name>/ entry — this
            # mimics the kernel's symlink target layout
            block_dir = slot_dir / "device" / "block" / device_name
            block_dir.mkdir(parents=True)


def test_discover_single_enclosure(tmp_path: Path) -> None:
    _make_enclosure(
        tmp_path,
        enc_name="0:0:32:0",
        vendor="DELL",
        product="PERC H710 Backplane",
        logical_id="0x5001e675500bf000",
        sg_name="sg3",
        slots=[
            (0, "OK", "sda"),
            (1, "OK", "sdb"),
            (2, "OK", "sdc"),
            (3, "Unknown", None),  # empty slot
            (4, "Unknown", None),
            (5, "Unknown", None),
            (6, "Unknown", None),
            (7, "Unknown", None),
        ],
    )
    enc_list = enclosures.discover_enclosures(sys_root=tmp_path)
    assert len(enc_list) == 1
    enc = enc_list[0]
    assert enc.vendor == "DELL"
    assert enc.product == "PERC H710 Backplane"
    assert enc.sg_device == "/dev/sg3"
    assert enc.slot_count == 8
    assert enc.populated_count == 3
    # Verify each populated slot has the correct device
    devices = {s.slot_number: s.device for s in enc.slots if s.populated}
    assert devices == {0: "/dev/sda", 1: "/dev/sdb", 2: "/dev/sdc"}


def test_discover_multi_enclosure_r720_plus_md1200(tmp_path: Path) -> None:
    _make_enclosure(
        tmp_path,
        enc_name="0:0:32:0",
        vendor="DELL",
        product="PERC H710",
        logical_id="0x5001e67500001000",
        sg_name="sg3",
        slots=[(i, "OK" if i < 5 else "Unknown", f"sd{chr(ord('a') + i)}" if i < 5 else None) for i in range(8)],
    )
    _make_enclosure(
        tmp_path,
        enc_name="1:0:10:0",
        vendor="DELL",
        product="MD1200",
        logical_id="0x5001e67500002000",
        sg_name="sg8",
        slots=[(i, "OK", f"sd{chr(ord('f') + i)}") for i in range(12)],
    )
    plan = enclosures.build_bay_plan(sys_root=tmp_path)
    assert plan.has_real_enclosures
    assert len(plan.enclosures) == 2
    assert plan.total_bays == 20
    assert plan.virtual_bay_count == 0
    labels = {e.label for e in plan.enclosures}
    assert "PERC H710" in labels
    assert "MD1200" in labels


def test_no_enclosures_falls_back_to_virtual(tmp_path: Path) -> None:
    # Empty sysfs tree — simulating consumer PC
    plan = enclosures.build_bay_plan(sys_root=tmp_path, virtual_bays_fallback=4)
    assert not plan.has_real_enclosures
    assert plan.enclosures == []
    assert plan.virtual_bay_count == 4
    assert plan.total_bays == 4


def test_virtual_bays_zero_when_fallback_zero(tmp_path: Path) -> None:
    plan = enclosures.build_bay_plan(sys_root=tmp_path, virtual_bays_fallback=0)
    assert plan.total_bays == 0


def test_slot_without_device_is_still_in_slot_list(tmp_path: Path) -> None:
    _make_enclosure(
        tmp_path,
        enc_name="0:0:32:0",
        vendor="D",
        product="X",
        logical_id="id",
        sg_name="sg0",
        slots=[(0, "Unknown", None), (1, "OK", "sda")],
    )
    [enc] = enclosures.discover_enclosures(sys_root=tmp_path)
    # Both slots present, only slot 1 populated
    assert [s.slot_number for s in enc.slots] == [0, 1]
    assert not enc.slots[0].populated
    assert enc.slots[1].populated
    assert enc.slots[1].device == "/dev/sda"
