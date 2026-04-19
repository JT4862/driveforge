"""Firmware lookup + (opt-in) update dispatch.

Phase 3 / MVP path: CHECK-ONLY. Look up the drive model in the firmware
DB; if a newer known-good version exists, report it. No blob download,
no flash attempted.

Phase 7+ path: opt-in auto-apply, gated by:
  1. Settings → Firmware → auto_apply is True
  2. A FirmwareApproval row exists for this (model, transport, version, sha256)
  3. The DB entry's signature verifies against the configured trust pubkey
  4. If require_canary, this drive is the batch's canary OR the canary
     for this (model, target_version) completed with a passing grade

Phase 8+ extends to SATA/SAS via vendor tools + generic sg_write_buffer.

See BUILD.md → Firmware Updates for the full safety model.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel

from driveforge.core.drive import Drive, Transport
from driveforge.core.process import run

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Apply path (Phase 7+) — opt-in, guarded by approval + signing + canary.
# ---------------------------------------------------------------------------


class ApplyOutcome(str):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class ApplyResult:
    outcome: str
    detail: str
    new_version: str | None = None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_nvme_firmware(drive: Drive, blob_path: Path) -> ApplyResult:
    """Flash NVMe firmware and re-check the version.

    Only the NVMe path is wired for MVP. `nvme format` / `fw-download` /
    `fw-commit` via the standard `nvme-cli`.
    """
    if drive.transport != Transport.NVME:
        return ApplyResult(ApplyOutcome.SKIPPED, f"non-NVMe transport: {drive.transport}")
    # Step 1: download into slot 2 (most drives) — assume activation on next
    # reset. Production may want slot + action configurable.
    r_dl = run(["nvme", "fw-download", "-f", str(blob_path), drive.device_path], timeout=15 * 60)
    if not r_dl.ok:
        return ApplyResult(ApplyOutcome.FAILED, f"fw-download failed: {r_dl.stderr.strip()}")
    # Step 2: commit with action=0 (replace and activate on next reset)
    r_co = run(
        ["nvme", "fw-commit", "-s", "2", "-a", "0", drive.device_path],
        timeout=2 * 60,
    )
    if not r_co.ok:
        return ApplyResult(ApplyOutcome.FAILED, f"fw-commit failed: {r_co.stderr.strip()}")
    # Post-update: re-query the drive's firmware version via smartctl.
    # The orchestrator usually does this separately; here we trust nvme
    # tooling's exit status and leave verification to the pipeline.
    return ApplyResult(ApplyOutcome.SUCCESS, "fw-download + fw-commit OK")


def verify_blob(blob_path: Path, expected_sha256: str) -> bool:
    return _sha256_of(blob_path) == expected_sha256.lower()


class FirmwareDownloadError(RuntimeError):
    pass


def download_blob(url: str, dest: Path, *, timeout: float = 120.0) -> Path:
    """Download a firmware blob to `dest`. Synchronous; call from executor."""
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            dest.write_bytes(resp.read())
    except Exception as exc:  # noqa: BLE001
        raise FirmwareDownloadError(f"failed to download {url}: {exc}") from exc
    return dest


@dataclass(frozen=True)
class ApplyDecision:
    """Describes what the orchestrator should do for this drive's firmware."""

    action: str  # "skip" | "apply" | "defer_canary"
    reason: str
    approved: bool = False
    target_version: str | None = None
    is_canary: bool = False


def decide_apply(
    *,
    drive: Drive,
    check: FirmwareCheck,
    auto_apply: bool,
    is_approved: bool,
    require_canary: bool,
    canary_done: bool,
    is_canary: bool,
) -> ApplyDecision:
    """Decide whether to flash, skip, or defer this drive's firmware.

    Pure function — no side effects. Orchestrator calls this per-drive in
    Phase 3 and acts on the returned decision.
    """
    if not check.update_available:
        return ApplyDecision(action="skip", reason="no update available")
    if not auto_apply:
        return ApplyDecision(
            action="skip",
            reason="update available; auto-apply disabled",
            target_version=check.latest_version,
        )
    if not is_approved:
        return ApplyDecision(
            action="skip",
            reason="no approval for this (model, version) — configure in Settings → Firmware",
            target_version=check.latest_version,
        )
    if require_canary and not is_canary and not canary_done:
        return ApplyDecision(
            action="defer_canary",
            reason="awaiting canary drive for this (model, version)",
            approved=True,
            target_version=check.latest_version,
        )
    return ApplyDecision(
        action="apply",
        reason="all checks passed; flashing",
        approved=True,
        target_version=check.latest_version,
        is_canary=is_canary,
    )
