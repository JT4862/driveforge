"""SAS-specific SMART parsing tests.

Uses a real smartctl --json -c -l selftest dump from JT's Seagate
ST300MM0006 on the R720 as the ground-truth fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

from driveforge.core.smart import parse_self_test_status

FIXTURE = Path(__file__).parent.parent / "fixtures" / "smartctl" / "sas_selftest_example.json"


def test_sas_log_with_most_recent_passed_returns_true() -> None:
    raw = FIXTURE.read_text()
    status = parse_self_test_status(raw)
    assert status.in_progress is False
    assert status.last_result_passed is True


def test_sas_in_progress_detected_via_result_value_15() -> None:
    fake = {
        "scsi_self_test_0": {
            "code": {"value": 1, "string": "Background short"},
            "result": {"value": 15, "string": "Self-test in progress..."},
        }
    }
    status = parse_self_test_status(json.dumps(fake))
    assert status.in_progress is True
    assert "in progress" in status.status_string.lower()


def test_sas_failure_result_flags_failed() -> None:
    fake = {
        "scsi_self_test_0": {
            "code": {"value": 1, "string": "Background short"},
            "result": {"value": 4, "string": "Self-test failed [segment 4]"},
        }
    }
    status = parse_self_test_status(json.dumps(fake))
    assert status.in_progress is False
    assert status.last_result_passed is False


def test_empty_payload_returns_neutral() -> None:
    status = parse_self_test_status("{}")
    assert status.in_progress is False
    assert status.last_result_passed is None


def test_malformed_json_returns_neutral() -> None:
    status = parse_self_test_status("not json at all")
    assert status.in_progress is False
    assert status.last_result_passed is None
