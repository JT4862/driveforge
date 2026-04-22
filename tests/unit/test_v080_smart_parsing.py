"""v0.8.0 — SmartSnapshot parsing of new attributes.

Verifies the transport-aware `_parse_lifetime_io_and_wear` and the
self-test-log parser. Synthetic smartctl JSON fixtures per transport.
"""

from __future__ import annotations

import json

from driveforge.core.smart import (
    NVME_DATA_UNIT_BYTES,
    SSD_WEAR_ATTR_CANDIDATES,
    _parse_lifetime_io_and_wear,
    _parse_self_test_log,
    parse,
)


# --------------------------------------------------------- NVMe parsing


def test_nvme_lifetime_io_uses_data_unit_constant() -> None:
    """NVMe data_units_* values are in 1000×512 byte units, NOT 1 MiB.
    This is the most-often-misread field in the spec — every third-party
    tool gets it wrong at least once. Pin the conversion."""
    data = {
        "nvme_smart_health_information_log": {
            "data_units_read": 1000,   # → 512 MB
            "data_units_written": 2000, # → 1.024 GB
            "percentage_used": 17,
            "available_spare": 100,
            "available_spare_threshold": 10,
            "critical_warning": 0,
            "media_errors": 0,
            "unsafe_shutdowns": 12,
        }
    }
    result = _parse_lifetime_io_and_wear(data, attrs=[])
    assert result["lifetime_host_reads_bytes"] == 1000 * NVME_DATA_UNIT_BYTES
    assert result["lifetime_host_writes_bytes"] == 2000 * NVME_DATA_UNIT_BYTES
    assert result["lifetime_host_reads_bytes"] == 512_000_000
    assert result["wear_pct_used"] == 17
    assert result["available_spare_pct"] == 100
    assert result["available_spare_threshold_pct"] == 10
    assert result["nvme_critical_warning"] == 0
    assert result["nvme_media_errors"] == 0
    assert result["nvme_unsafe_shutdowns"] == 12


def test_nvme_critical_warning_bitfield_propagates() -> None:
    """Any bit in critical_warning is a drive firmware alert. Parser
    must pass the raw bitfield through so the grading layer can
    inspect it (not just boolean-coerce to 'has any warning')."""
    data = {
        "nvme_smart_health_information_log": {
            "critical_warning": 0x04,  # reliability-degraded bit
            "media_errors": 5,
        }
    }
    result = _parse_lifetime_io_and_wear(data, attrs=[])
    assert result["nvme_critical_warning"] == 0x04
    assert result["nvme_media_errors"] == 5


# ---------------------------------------------------------- SAS parsing


def test_sas_lifetime_io_uses_error_counter_log() -> None:
    """SAS drives report lifetime I/O as `bytes_processed` in the SCSI
    error-counter-log. Bytes native (no unit conversion)."""
    data = {
        "scsi_error_counter_log": {
            "read":  {"bytes_processed": 1_234_567_890},
            "write": {"bytes_processed": 987_654_321},
        }
    }
    result = _parse_lifetime_io_and_wear(data, attrs=[])
    assert result["lifetime_host_reads_bytes"] == 1_234_567_890
    assert result["lifetime_host_writes_bytes"] == 987_654_321
    # SAS doesn't expose NVMe-specific fields or reliable wear %
    assert result["wear_pct_used"] is None
    assert result["nvme_critical_warning"] is None


def test_sas_path_takes_precedence_over_absent_nvme() -> None:
    """When NVMe subtree is missing but SCSI log is present, SAS path
    fires. Order in the dispatcher matters."""
    data = {
        "scsi_error_counter_log": {
            "write": {"bytes_processed": 500},
        }
    }
    result = _parse_lifetime_io_and_wear(data, attrs=[])
    assert result["lifetime_host_writes_bytes"] == 500


# ---------------------------------------------------------- SATA parsing


def _fake_attr(attr_id: int, value: int | None = None, raw: int | None = None):
    from driveforge.core.smart import SmartAttribute
    return SmartAttribute(id=attr_id, name=f"attr_{attr_id}", value=value, raw_value=raw)


def test_sata_lifetime_io_multiplies_lbas_by_block_size() -> None:
    """Attr 241/242 carry raw LBA counts. Bytes = LBAs × logical_block_size.
    512 is the common default but 4Kn drives report 4096."""
    # 512-byte sectors
    data = {"logical_block_size": 512}
    attrs = [_fake_attr(241, raw=1_000_000), _fake_attr(242, raw=2_000_000)]
    result = _parse_lifetime_io_and_wear(data, attrs)
    assert result["lifetime_host_writes_bytes"] == 1_000_000 * 512
    assert result["lifetime_host_reads_bytes"] == 2_000_000 * 512

    # 4Kn sectors
    data_4k = {"logical_block_size": 4096}
    attrs_4k = [_fake_attr(241, raw=1_000_000)]
    result_4k = _parse_lifetime_io_and_wear(data_4k, attrs_4k)
    assert result_4k["lifetime_host_writes_bytes"] == 1_000_000 * 4096


def test_sata_lifetime_io_falls_back_to_512_on_missing_block_size() -> None:
    """Drives that don't report logical_block_size in the JSON should
    default to 512 (spec baseline) rather than crashing."""
    data = {}  # no logical_block_size key
    attrs = [_fake_attr(241, raw=1000)]
    result = _parse_lifetime_io_and_wear(data, attrs)
    assert result["lifetime_host_writes_bytes"] == 1000 * 512


def test_sata_ssd_wear_from_vendor_attribute() -> None:
    """Each of the four vendor wear attributes (233/177/231/169) reports
    a normalized REMAINING life in the `value` field. Parser converts
    to wear_pct_used via (100 - remaining). First present attribute
    wins."""
    # Intel 233 @ remaining=80 → wear=20%
    data = {}
    attrs = [_fake_attr(233, value=80)]
    result = _parse_lifetime_io_and_wear(data, attrs)
    assert result["wear_pct_used"] == 20

    # Samsung 177 @ remaining=55 → wear=45%
    attrs = [_fake_attr(177, value=55)]
    result = _parse_lifetime_io_and_wear(data, attrs)
    assert result["wear_pct_used"] == 45

    # Crucial 169 @ remaining=95 → wear=5%
    attrs = [_fake_attr(169, value=95)]
    result = _parse_lifetime_io_and_wear(data, attrs)
    assert result["wear_pct_used"] == 5


def test_sata_wear_first_candidate_wins() -> None:
    """When multiple wear attributes are present (vendors occasionally
    populate more than one), the first candidate in
    SSD_WEAR_ATTR_CANDIDATES order is the winner. Tests the deliberate
    preference order (Intel 233 first, Samsung 177 second, etc.)."""
    # Both 233 and 177 present with different values.
    attrs = [_fake_attr(177, value=40), _fake_attr(233, value=70)]
    result = _parse_lifetime_io_and_wear({}, attrs)
    # 233 is first in the candidate order → wins with wear=30
    assert result["wear_pct_used"] == 30
    # Sanity: SSD_WEAR_ATTR_CANDIDATES order is what we expect
    assert SSD_WEAR_ATTR_CANDIDATES[0] == 233


def test_hdd_has_no_wear_attribute() -> None:
    """HDDs don't expose wear percentage. Empty attrs → None."""
    result = _parse_lifetime_io_and_wear({}, attrs=[])
    assert result["wear_pct_used"] is None


# ------------------------------------------------------- self-test log


def test_self_test_log_summarizes_pass_history() -> None:
    """No past failure → has_past_failure is False, total_count is the
    length of the table."""
    data = {
        "ata_smart_self_test_log": {
            "standard": {
                "table": [
                    {"status": {"passed": True}, "type": {"string": "Short"}, "lifetime_hours": 100},
                    {"status": {"passed": True}, "type": {"string": "Extended"}, "lifetime_hours": 200},
                    {"status": {"passed": True}, "type": {"string": "Short"}, "lifetime_hours": 300},
                ]
            }
        }
    }
    result = _parse_self_test_log(data)
    assert result["self_test_total_count"] == 3
    assert result["self_test_has_past_failure"] is False
    assert result["self_test_last_failed_at_hour"] is None


def test_self_test_log_captures_most_recent_failure_hour() -> None:
    """When a past failure exists, the parser pulls the lifetime_hours
    from the first (most recent, per spec) failed entry."""
    data = {
        "ata_smart_self_test_log": {
            "standard": {
                "table": [
                    {"status": {"passed": True}, "type": {"string": "Short"}, "lifetime_hours": 500},
                    {"status": {"passed": False}, "type": {"string": "Extended"}, "lifetime_hours": 420},
                    {"status": {"passed": True}, "type": {"string": "Short"}, "lifetime_hours": 300},
                ]
            }
        }
    }
    result = _parse_self_test_log(data)
    assert result["self_test_has_past_failure"] is True
    assert result["self_test_last_failed_at_hour"] == 420


def test_self_test_log_empty_returns_none_fields() -> None:
    """No log at all (some drives don't record one) returns all None,
    not False/0 — there's a meaningful distinction between 'we know
    there are zero failures' and 'we don't have the data.'"""
    result = _parse_self_test_log({})
    assert result["self_test_total_count"] is None
    assert result["self_test_has_past_failure"] is None


# ----------------------------------------------------- integration test


def test_parse_integration_sata_ssd_with_full_attrs() -> None:
    """End-to-end: parse a realistic-shape smartctl JSON for a SATA SSD
    and confirm every new field populates."""
    payload = {
        "device": {"name": "/dev/sdx"},
        "model_name": "Samsung SSD 860 EVO 500GB",
        "serial_number": "S3Z9NX0N123456",
        "firmware_version": "RVT04B6Q",
        "smart_status": {"passed": True},
        "temperature": {"current": 35},
        "power_on_time": {"hours": 15_000},
        "logical_block_size": 512,
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0}, "value": 100},
                {"id": 9, "name": "Power_On_Hours", "raw": {"value": 15000}, "value": 99},
                {"id": 177, "name": "Wear_Leveling_Count", "raw": {"value": 42}, "value": 85},
                {"id": 184, "name": "End_to_End_Error", "raw": {"value": 0}, "value": 100},
                {"id": 188, "name": "Command_Timeout", "raw": {"value": 2}, "value": 100},
                {"id": 194, "name": "Temperature_Celsius", "raw": {"value": 35}, "value": 65},
                {"id": 196, "name": "Reallocation_Event_Count", "raw": {"value": 0}, "value": 100},
                {"id": 199, "name": "UDMA_CRC_Error_Count", "raw": {"value": 7}, "value": 200},
                {"id": 241, "name": "Total_LBAs_Written", "raw": {"value": 1_000_000_000}, "value": 100},
                {"id": 242, "name": "Total_LBAs_Read",    "raw": {"value": 5_000_000_000}, "value": 100},
            ]
        },
        "ata_smart_self_test_log": {"standard": {"table": []}},
    }
    snap = parse(json.dumps(payload), device="/dev/sdx")

    assert snap.power_on_hours == 15_000
    assert snap.wear_pct_used == 15  # 100 - 85
    assert snap.lifetime_host_writes_bytes == 1_000_000_000 * 512
    assert snap.lifetime_host_reads_bytes == 5_000_000_000 * 512
    assert snap.end_to_end_error_count == 0
    assert snap.command_timeout_count == 2
    assert snap.reallocation_event_count == 0
    assert snap.udma_crc_error_count == 7
    # NVMe-only fields absent
    assert snap.nvme_critical_warning is None
    assert snap.nvme_media_errors is None


def test_parse_integration_nvme() -> None:
    """End-to-end: NVMe payload populates NVMe-specific fields + lifetime I/O."""
    payload = {
        "device": {"name": "/dev/nvme0n1"},
        "model_name": "Samsung SSD 970 EVO 1TB",
        "serial_number": "S5H9NF1M12345",
        "firmware_version": "2B2QEXE7",
        "smart_status": {"passed": True},
        "temperature": {"current": 40},
        "power_on_time": {"hours": 8_000},
        "nvme_smart_health_information_log": {
            "critical_warning": 0,
            "temperature": 313,  # Kelvin
            "available_spare": 100,
            "available_spare_threshold": 10,
            "percentage_used": 8,
            "data_units_read": 10_000,
            "data_units_written": 5_000,
            "host_reads": 1_234_567,
            "host_writes": 890_123,
            "controller_busy_time": 123,
            "power_cycles": 50,
            "power_on_hours": 8000,
            "unsafe_shutdowns": 3,
            "media_errors": 0,
            "num_err_log_entries": 0,
        },
    }
    snap = parse(json.dumps(payload), device="/dev/nvme0n1")
    assert snap.wear_pct_used == 8
    assert snap.available_spare_pct == 100
    assert snap.available_spare_threshold_pct == 10
    assert snap.nvme_critical_warning == 0
    assert snap.nvme_media_errors == 0
    assert snap.nvme_unsafe_shutdowns == 3
    assert snap.lifetime_host_reads_bytes == 10_000 * NVME_DATA_UNIT_BYTES
    assert snap.lifetime_host_writes_bytes == 5_000 * NVME_DATA_UNIT_BYTES
