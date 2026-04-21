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
    # v0.6.4+ auto-print on pipeline completion. When True and a
    # printer is configured, the orchestrator fires print_label
    # automatically at the end of every drive's pipeline (pass OR
    # fail tier — both produce a sticker with the grade). Operators
    # can still click Print Label manually for a reprint. Default
    # True because that's the whole point of having a printer
    # configured: you want labels without extra clicks.
    auto_print: bool = True


class IntegrationsConfig(BaseModel):
    webhook_url: str | None = None
    cloudflare_tunnel_hostname: str | None = None


class GradingConfig(BaseModel):
    """A/B/C/Fail thresholds. Users tune in Settings → Grading.

    Values here are the shipped defaults; `/etc/driveforge/grading.yaml` can
    override on a per-install basis.
    """

    # Reallocated-sector thresholds. Grade A used to require strictly 0, but
    # that's over-strict: every commercial drive ships with a spare-sector pool
    # and a handful of stable reallocations has no correlation with imminent
    # failure (Backblaze multi-year data). The `no_degradation_reallocated_sectors`
    # rule already fails a drive that reallocates MORE during its own test, so
    # the absolute count just gates the initial bucketing. 3 is a good "pristine
    # with minor wear" ceiling for Grade A.
    grade_a_reallocated_max: int = 3
    grade_b_reallocated_max: int = 8
    grade_c_reallocated_max: int = 40
    fail_on_pending_sectors: bool = True
    fail_on_offline_uncorrectable: bool = True
    thermal_excursion_c: int | None = 60  # None = disable
    power_on_hours_drift_tolerance_h: int = 1

    # v0.5.6+ throughput-consistency grading during badblocks.
    # Deliberately self-referential (drive vs itself, not drive vs
    # benchmark table) — benchmark tables go stale the moment new
    # SKUs ship. See driveforge/core/throughput.py for the design
    # rationale.
    #
    # within_pass_variance_ratio:
    #   If p5 of write throughput during any pass is below this fraction
    #   of the pass mean, the drive had significant slowdowns mid-pass
    #   (classic signal of sectors being recovered via internal ECC
    #   retry). Demotes one tier: A → B, B → C, C → F.
    #   Default 0.25 = "p5 must be at least 25% of the mean."
    #
    # pass_to_pass_degradation_ratio:
    #   If pass N mean drops below this fraction of pass 2 mean (pass 1
    #   is skipped to avoid false-firing on SLC-cache exhaustion in
    #   consumer SSDs), the drive actively degraded during burn-in.
    #   F-tier.
    #   Default 0.70 = "last pass must hold at least 70% of pass-2's
    #   speed."
    #
    # None on either disables that specific rule while keeping the
    # other active.
    within_pass_variance_ratio: float | None = 0.25
    pass_to_pass_degradation_ratio: float | None = 0.70


class DaemonConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    state_dir: Path = STATE_DIR_DEFAULT
    log_dir: Path = LOG_DIR_DEFAULT
    db_path: Path = STATE_DIR_DEFAULT / "driveforge.db"
    pending_labels_dir: Path = STATE_DIR_DEFAULT / "pending-labels"
    reports_dir: Path = STATE_DIR_DEFAULT / "reports"

    # Auto-enrollment on drive insert. When set to "quick" or "full", the
    # hotplug add handler automatically starts a single-drive batch for
    # any freshly-inserted drive that doesn't have a recent completed
    # run. Intended for unattended rack workflows — operator watches LED
    # patterns, pulls pass/fail drives, drops in new ones, and DriveForge
    # runs the whole pipeline (including auto-printing a cert label if a
    # printer is configured) with no dashboard interaction.
    #   "off"   — default; drives only run when New Batch is clicked
    #   "quick" — auto quick-mode (skips badblocks + long self-test)
    #   "full"  — auto full pipeline
    # Drives already passed within the last hour are NOT re-enrolled on
    # re-insert (so you can freely yank a Grade-A drive and plug it back
    # without restarting its test).
    auto_enroll_mode: str = "off"
    # Root path for sysfs — overridable in tests + dev to point at a
    # synthetic tree. Production leaves this at "/".
    sysfs_root: Path = Path("/")

    # v0.5.5+ — What to do when a quick-pass run finishes with triage=fail
    # (the drive deteriorated during the quick pass). Three modes:
    #   "badge_only" (default)  — show the Fail triage badge and leave
    #                             the next step to the operator.
    #   "prompt"                — surface a dashboard prompt offering to
    #                             run a full pipeline on the drive.
    #   "auto_promote"          — automatically queue a full-pipeline run
    #                             once the failed quick pass completes.
    # Conservative default (badge_only) matches DriveForge's appliance
    # philosophy: no silent auto-escalation without operator consent.
    quick_pass_fail_action: str = "badge_only"

    # v0.5.5+ — How often (in seconds) to sample per-drive temperature +
    # chassis power during active runs. Each active drive gets its own
    # background sampler; samples land in the telemetry_samples table
    # and feed the drive-detail telemetry charts.
    #
    # Pre-v0.5.5 the orchestrator only sampled at SMART-snapshot phase
    # boundaries (twice per run total), producing the sparse 2-sample
    # charts on multi-hour runs. 30 s is the sweet spot: enough
    # resolution to see the warm-up curve and thermal plateau during
    # badblocks, cheap enough to not hammer smartctl while the drive
    # is under heavy I/O.
    telemetry_sample_interval_s: int = 30


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
