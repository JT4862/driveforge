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


# ----- SAS-layer detection (no SES) ---------------------------------------
#
# Fallback path for hardware with a SAS expander but no SES target. Mimics
# the sysfs layout the kernel's mpt3sas / similar drivers build for an
# expander-backed backplane:
#
#   /sys/class/sas_device/<name> → symlink into /sys/devices/.../
#   /sys/devices/.../expander-0:0/
#     ├── device/vendor, model          (expander chip identity)
#     ├── sas_device/expander-0:0/
#     │     ├── sas_address
#     │     ├── enclosure_identifier    (empty — it IS the enclosure)
#     │     └── device_type             "edge expander"
#     ├── port-0:0:0/end_device-0:0:0/
#     │     ├── sas_device/end_device-0:0:0/
#     │     │     ├── sas_address
#     │     │     ├── bay_identifier
#     │     │     └── enclosure_identifier    (= expander sas_address)
#     │     └── target0:0:0/0:0:0:0/block/sdX/
#     └── port-0:0:2/end_device-0:0:2/... (another drive)


def _make_sas_expander_and_drives(
    sys_root: Path,
    *,
    expander_name: str = "expander-0:0",
    expander_sas_addr: str,
    expander_vendor: str = "LSI",
    expander_model: str = "SAS2x36",
    drives: list[tuple[str, str, int, str | None]],  # (name, sas_addr, bay, block_device)
) -> None:
    """Build a synthetic SAS-layer sysfs tree at `sys_root`.

    drives: list of (end_device_name, sas_address, bay_identifier, block_device).
    A None block_device simulates an empty slot the expander advertised.
    """
    # Root of the topology lives at /sys/devices/pci.../host0/port-0:0/<expander>/
    topo_root = sys_root / "sys" / "devices" / "pci0000:00" / "0000:03:00.0" / "host0" / "port-0:0" / expander_name

    # Expander chip identity (vendor/model readable via device/)
    (topo_root / "device").mkdir(parents=True)
    (topo_root / "device" / "vendor").write_text(expander_vendor)
    (topo_root / "device" / "model").write_text(expander_model)

    # The expander's own sas_device entry
    exp_sas = topo_root / "sas_device" / expander_name
    exp_sas.mkdir(parents=True)
    (exp_sas / "sas_address").write_text(expander_sas_addr)
    (exp_sas / "enclosure_identifier").write_text("")  # expander IS the enclosure
    (exp_sas / "device_type").write_text("edge expander")

    # Class symlink for the expander
    class_root = sys_root / "sys" / "class" / "sas_device"
    class_root.mkdir(parents=True, exist_ok=True)
    (class_root / expander_name).symlink_to(exp_sas)

    # Each end_device drive
    for name, sas_addr, bay, block_device in drives:
        # port number is the last octet of the end_device name (e.g. "0:0:2" → 2)
        port_suffix = name.rsplit("-", 1)[-1]  # "0:0:2"
        port_dir = topo_root / f"port-{port_suffix}"
        ed_dir = port_dir / name
        ed_dir.mkdir(parents=True)

        # Inner sas_device namespace (where bay_identifier etc. live)
        ed_sas = ed_dir / "sas_device" / name
        ed_sas.mkdir(parents=True)
        (ed_sas / "sas_address").write_text(sas_addr)
        (ed_sas / "bay_identifier").write_text(str(bay))
        (ed_sas / "enclosure_identifier").write_text(expander_sas_addr)
        (ed_sas / "phy_identifier").write_text(str(bay))

        # Class symlink for this end_device
        (class_root / name).symlink_to(ed_sas)

        # Block device (only for populated slots)
        if block_device is not None:
            # target<host>:<channel>:<target>/<lun>/block/<name>
            host, channel, target = port_suffix.split(":")
            block_parent = ed_dir / f"target{host}:{channel}:{target}" / f"{host}:{channel}:{target}:0" / "block" / block_device
            block_parent.mkdir(parents=True)


def test_sas_layer_detects_expander_only_backplane(tmp_path: Path) -> None:
    # NX-3200-style: LSI SAS2308 HBA + edge expander backplane, no SES target.
    # Two drives in bays 2 and 12; one advertised-but-empty slot at bay 24.
    _make_sas_expander_and_drives(
        tmp_path,
        expander_sas_addr="0x500056b36789abff",
        expander_vendor="LSI",
        expander_model="SAS2x36",
        drives=[
            ("end_device-0:0:0", "0x500056b36789abea", 12, "sdb"),
            ("end_device-0:0:1", "0x500056b36789abfd", 24, None),  # empty slot
            ("end_device-0:0:2", "0x500056b36789abe8", 2, "sda"),
        ],
    )
    enc_list = enclosures.discover_sas_enclosures(sys_root=tmp_path)
    assert len(enc_list) == 1
    enc = enc_list[0]
    # Expander metadata surfaces as the enclosure label
    assert enc.vendor == "LSI"
    assert enc.product == "SAS2x36"
    assert enc.logical_id == "0x500056b36789abff"
    # No SES target → no sg_device → LED control unavailable
    assert enc.sg_device is None
    # All three end_devices became slots, sorted by bay
    assert [s.slot_number for s in enc.slots] == [2, 12, 24]
    # Populated flags track presence of a backing block device
    by_bay = {s.slot_number: s for s in enc.slots}
    assert by_bay[2].populated and by_bay[2].device == "/dev/sda"
    assert by_bay[12].populated and by_bay[12].device == "/dev/sdb"
    assert not by_bay[24].populated and by_bay[24].device is None


def test_build_bay_plan_prefers_sas_when_ses_empty(tmp_path: Path) -> None:
    # No SES enclosures; SAS layer has one expander with one drive.
    _make_sas_expander_and_drives(
        tmp_path,
        expander_sas_addr="0x500056b36789abff",
        drives=[("end_device-0:0:0", "0x500056b36789abea", 5, "sda")],
    )
    plan = enclosures.build_bay_plan(sys_root=tmp_path)
    assert plan.has_real_enclosures
    assert plan.virtual_bay_count == 0
    assert plan.total_bays == 1
    assert plan.enclosures[0].slots[0].slot_number == 5


def test_build_bay_plan_dedupes_when_ses_and_sas_both_present(tmp_path: Path) -> None:
    # Unusual but defensive: a host where BOTH paths see the same backplane
    # (SES as primary, SAS as redundant). We shouldn't double-count the same
    # enclosure.
    ses_logical_id = "0x500056b36789abff"
    _make_enclosure(
        tmp_path,
        enc_name="0:0:32:0",
        vendor="DELL",
        product="Chassis",
        logical_id=ses_logical_id,
        sg_name="sg3",
        slots=[(0, "OK", "sda"), (1, "OK", "sdb")],
    )
    _make_sas_expander_and_drives(
        tmp_path,
        expander_sas_addr=ses_logical_id,  # same logical id → should be deduped
        drives=[
            ("end_device-0:0:0", "0x500056b36789abea", 0, "sda"),
            ("end_device-0:0:1", "0x500056b36789abeb", 1, "sdb"),
        ],
    )
    plan = enclosures.build_bay_plan(sys_root=tmp_path)
    # Only the SES enclosure should survive the dedupe
    assert len(plan.enclosures) == 1
    assert plan.enclosures[0].sg_device == "/dev/sg3"
    assert plan.total_bays == 2


def test_sas_end_device_without_enclosure_identifier_is_skipped(tmp_path: Path) -> None:
    # Direct-attach SAS with no expander — end_devices exist but have no
    # enclosure_identifier. Shouldn't produce a fake single-drive enclosure.
    class_root = tmp_path / "sys" / "class" / "sas_device"
    real_dir = tmp_path / "sys" / "devices" / "pci0000:00" / "host0" / "end_device-0:0:0"
    (real_dir / "sas_device" / "end_device-0:0:0").mkdir(parents=True)
    (real_dir / "sas_device" / "end_device-0:0:0" / "sas_address").write_text("0x5000c500abcdef01")
    (real_dir / "sas_device" / "end_device-0:0:0" / "bay_identifier").write_text("")
    (real_dir / "sas_device" / "end_device-0:0:0" / "enclosure_identifier").write_text("")
    class_root.mkdir(parents=True)
    (class_root / "end_device-0:0:0").symlink_to(real_dir / "sas_device" / "end_device-0:0:0")

    assert enclosures.discover_sas_enclosures(sys_root=tmp_path) == []
