"""Firmware lookup + (opt-in) update dispatch.

MVP scope (Phase 3): CHECK-ONLY for NVMe. Look up the drive model in the
firmware DB; if a newer known-good version exists, report it via the API
and UI. No blob is downloaded, no flash is attempted.

Phase 7 will add the opt-in auto-apply path with signing + dry-run + post-
update re-check. Phase 8+ extends the DB to SATA/SAS.

See BUILD.md → Firmware Updates for the full safety model.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from driveforge.core.drive import Drive, Transport


class FirmwareEntry(BaseModel):
    """One row in the firmware lookup DB.

    The DB is a YAML file shipped with DriveForge; users can override via
    Settings → Integrations → Firmware DB URL.
    """

    model: str
    transport: Transport
    version: str
    blob_url: str | None = None
    sha256: str | None = None
    signature: str | None = None  # detached signature, required before apply
    notes: str = ""


class FirmwareCheck(BaseModel):
    current_version: str | None
    latest_version: str | None
    update_available: bool
    entry: FirmwareEntry | None = None
    reason: str = ""  # e.g. "no DB entry", "vendor-gated", "OEM-branded"


OEM_PREFIXES = ("DELL-", "HP-", "HPE-", "NETAPP-", "IBM-")


def _load_db(path: Path) -> list[FirmwareEntry]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [FirmwareEntry.model_validate(e) for e in data.get("entries", [])]


def is_oem_branded(model: str) -> bool:
    return any(model.upper().startswith(p) for p in OEM_PREFIXES)


def check_firmware(drive: Drive, *, db_path: Path) -> FirmwareCheck:
    """Look up a drive in the firmware DB and report whether an update exists.

    Never flashes. Never downloads. Returns a read-only assessment.
    """
    if is_oem_branded(drive.model):
        return FirmwareCheck(
            current_version=drive.firmware_version,
            latest_version=None,
            update_available=False,
            reason="OEM-branded drive — retail firmware cannot be applied",
        )
    entries = _load_db(db_path)
    candidates = [e for e in entries if e.model == drive.model and e.transport == drive.transport]
    if not candidates:
        return FirmwareCheck(
            current_version=drive.firmware_version,
            latest_version=None,
            update_available=False,
            reason="no DB entry for this model",
        )
    # Take the lexically-latest version string (DB is expected to be curated;
    # a real semver comparator can replace this when we have real data).
    latest = sorted(candidates, key=lambda e: e.version)[-1]
    available = (
        drive.firmware_version is not None
        and latest.version > drive.firmware_version
    )
    return FirmwareCheck(
        current_version=drive.firmware_version,
        latest_version=latest.version,
        update_available=available,
        entry=latest,
        reason="update available" if available else "drive is at latest known-good version",
    )
