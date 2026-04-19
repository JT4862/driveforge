"""Config loader.

Settings come from three layers, lowest precedence first:
1. Packaged defaults (this module)
2. `/etc/driveforge/driveforge.yaml` (daemon-writable)
3. Env vars prefixed with `DRIVEFORGE_`

The file on disk is written by the daemon when the user changes settings in
the UI. Users should not hand-edit it; see BUILD.md → appliance philosophy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


CONFIG_PATH_DEFAULT = Path("/etc/driveforge/driveforge.yaml")
STATE_DIR_DEFAULT = Path("/var/lib/driveforge")
LOG_DIR_DEFAULT = Path("/var/log/driveforge")


class PrinterConfig(BaseModel):
    """Printer settings. None = no printer configured yet."""

    model: str | None = None
    connection: str = "usb"  # usb | network | bluetooth
    backend_identifier: str | None = None  # e.g. "file:///tmp/labels/" in dev
    label_roll: str | None = None  # e.g. "DK-1209"


class IntegrationsConfig(BaseModel):
    webhook_url: str | None = None
    cloudflare_tunnel_hostname: str | None = None
    firmware_db_url: str | None = None  # defaults to bundled DB


class FirmwareConfig(BaseModel):
    """Firmware auto-apply behavior.

    Never defaults to True — flashing is opt-in and requires per-entry
    approval in Settings → Firmware regardless of this toggle.
    """

    auto_apply: bool = False
    # Base64-encoded Ed25519 public key. Empty = use bundled trust root.
    trust_pubkey: str = ""
    # Always test the first drive of a new (model, target_version) flash
    # inside a batch, wait for its grade, then proceed with siblings only
    # if the canary passed.
    require_canary: bool = True


class GradingConfig(BaseModel):
    """A/B/C/Fail thresholds. Users tune in Settings → Grading.

    Values here are the shipped defaults; `/etc/driveforge/grading.yaml` can
    override on a per-install basis.
    """

    grade_a_reallocated_max: int = 0
    grade_b_reallocated_max: int = 8
    grade_c_reallocated_max: int = 40
    fail_on_pending_sectors: bool = True
    fail_on_offline_uncorrectable: bool = True
    thermal_excursion_c: int | None = 60  # None = disable
    power_on_hours_drift_tolerance_h: int = 1


class DaemonConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    state_dir: Path = STATE_DIR_DEFAULT
    log_dir: Path = LOG_DIR_DEFAULT
    db_path: Path = STATE_DIR_DEFAULT / "driveforge.db"
    pending_labels_dir: Path = STATE_DIR_DEFAULT / "pending-labels"
    reports_dir: Path = STATE_DIR_DEFAULT / "reports"

    # Hardware / bays
    # Fallback slot count when no SES enclosure is detected (consumer PC,
    # NVMe-only host, etc.). Ignored when real enclosures are present.
    virtual_bays: int = 8
    # Root path for sysfs — overridable in tests + dev to point at a
    # synthetic tree. Production leaves this at "/".
    sysfs_root: Path = Path("/")


class Settings(BaseSettings):
    """Top-level config. Loaded from disk + env."""

    model_config = SettingsConfigDict(
        env_prefix="DRIVEFORGE_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    printer: PrinterConfig = Field(default_factory=PrinterConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    firmware: FirmwareConfig = Field(default_factory=FirmwareConfig)
    grading: GradingConfig = Field(default_factory=GradingConfig)

    # First-run state. Flipped to True when the wizard completes; user can
    # flip back to False from Settings to replay the wizard.
    setup_completed: bool = False

    # Dev-only
    dev_mode: bool = False
    fixtures_dir: Path | None = None


def load(config_path: Path | None = None) -> Settings:
    """Load settings from disk + env. Missing file = pure defaults."""
    path = config_path or CONFIG_PATH_DEFAULT
    file_values: dict[str, Any] = {}
    if path.exists():
        with path.open() as f:
            file_values = yaml.safe_load(f) or {}
    return Settings(**file_values)


def save(settings: Settings, config_path: Path | None = None) -> None:
    """Persist settings back to disk. Called from the Settings UI only."""
    path = config_path or CONFIG_PATH_DEFAULT
    path.parent.mkdir(parents=True, exist_ok=True)
    data = settings.model_dump(mode="json", exclude={"dev_mode", "fixtures_dir"})
    with path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
