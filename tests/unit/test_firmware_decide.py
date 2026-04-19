from __future__ import annotations

from driveforge.core import firmware
from driveforge.core.drive import Drive, Transport


def _drive() -> Drive:
    return Drive(
        serial="S1",
        model="Samsung SSD 970 EVO Plus 1TB",
        capacity_bytes=1_000_000_000_000,
        transport=Transport.NVME,
        device_path="/dev/nvme0n1",
        firmware_version="2B2QEXM7",
    )


def _check(available: bool = True, latest: str = "3B2QEXM7") -> firmware.FirmwareCheck:
    return firmware.FirmwareCheck(
        current_version="2B2QEXM7",
        latest_version=latest if available else None,
        update_available=available,
        reason="",
    )


def test_no_update_means_skip() -> None:
    d = firmware.decide_apply(
        drive=_drive(),
        check=_check(available=False),
        auto_apply=True,
        is_approved=True,
        require_canary=False,
        canary_done=False,
        is_canary=False,
    )
    assert d.action == "skip"


def test_auto_apply_off_means_skip() -> None:
    d = firmware.decide_apply(
        drive=_drive(),
        check=_check(),
        auto_apply=False,
        is_approved=True,
        require_canary=False,
        canary_done=False,
        is_canary=False,
    )
    assert d.action == "skip"
    assert "auto-apply disabled" in d.reason


def test_unapproved_entry_means_skip() -> None:
    d = firmware.decide_apply(
        drive=_drive(),
        check=_check(),
        auto_apply=True,
        is_approved=False,
        require_canary=False,
        canary_done=False,
        is_canary=False,
    )
    assert d.action == "skip"
    assert "no approval" in d.reason


def test_non_canary_defers_when_canary_pending() -> None:
    d = firmware.decide_apply(
        drive=_drive(),
        check=_check(),
        auto_apply=True,
        is_approved=True,
        require_canary=True,
        canary_done=False,
        is_canary=False,
    )
    assert d.action == "defer_canary"


def test_canary_applies_first() -> None:
    d = firmware.decide_apply(
        drive=_drive(),
        check=_check(),
        auto_apply=True,
        is_approved=True,
        require_canary=True,
        canary_done=False,
        is_canary=True,
    )
    assert d.action == "apply"
    assert d.is_canary


def test_sibling_applies_after_canary_done() -> None:
    d = firmware.decide_apply(
        drive=_drive(),
        check=_check(),
        auto_apply=True,
        is_approved=True,
        require_canary=True,
        canary_done=True,
        is_canary=False,
    )
    assert d.action == "apply"
    assert not d.is_canary
