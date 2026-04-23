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
    connection: str = "usb"  # usb | network | bluetooth | file
    backend_identifier: str | None = None  # e.g. "file:///tmp/labels/" in dev
    label_roll: str | None = None  # e.g. "DK-1209"
    # v0.7.0+ Network-backend fields. Only used when connection=="network";
    # `network_host` is an IPv4 / hostname the printer listens on, and
    # `network_port` is the TCP port (Brother QLs with WiFi/Ethernet
    # speak brother_ql's raw raster protocol on port 9100 by default
    # — same convention as LPR/RAW). On save, the Settings handler
    # synthesizes `backend_identifier = f"tcp://{host}:{port}"` from
    # these so the lower-level brother_ql call site doesn't need to
    # know about two sources of truth. Round-tripping these in
    # PrinterConfig means the Settings form remembers them across
    # edits (pre-v0.7.0 we'd have to re-parse them out of
    # backend_identifier on every render).
    network_host: str | None = None
    network_port: int = 9100
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

    # v0.8.0+ Age-based ceilings. Applied AFTER the base grade is
    # computed. A drive can only be DEMOTED by a ceiling; if the base
    # grade is already below the ceiling, no change. Thresholds are in
    # power-on hours (24/7 = 8760 h/year).
    #   poh_a_ceiling_hours: above this, can't be A (default ~4 years)
    #   poh_b_ceiling_hours: above this, can't be B (default ~7 years)
    #   poh_fail_hours:      above this, force-F (None = disabled)
    age_ceiling_enabled: bool = True
    poh_a_ceiling_hours: int = 35040
    poh_b_ceiling_hours: int = 61320
    poh_fail_hours: int | None = None

    # v0.8.0+ Workload ceilings — compare lifetime host writes to the
    # drive's class-rated TBW. Writes-beyond-rated-workload is the
    # strongest single indicator of "drive has done more than its
    # design life." Ceilings below are percentages of the rated TBW
    # for the drive's auto-detected class (enterprise/consumer × HDD/SSD).
    workload_ceiling_enabled: bool = True
    workload_a_ceiling_pct: int = 60    # > 60% of rated → can't be A
    workload_b_ceiling_pct: int = 100   # > 100% of rated → can't be B
    workload_fail_pct: int = 150        # > 150% of rated → force-F
    # Rated lifetime host writes in TB, per drive class. The classifier
    # (driveforge/core/drive_class.py) maps each drive to one of these
    # buckets. Defaults reflect typical-market 5-year design-life:
    #   Enterprise HDD @ 550 TB/yr × 5 yr = 2750 TB
    #   Enterprise SSD @ ~730 TB/yr × 5 yr = 3650 TB (mid-range DC)
    #   Consumer  HDD @ 55 TB/yr × 5 yr = 275 TB
    #   Consumer  SSD @ ~120 TB/yr × 5 yr = 600 TB (typical mainstream)
    rated_tbw_enterprise_hdd: int = 2750
    rated_tbw_enterprise_ssd: int = 3650
    rated_tbw_consumer_hdd: int = 275
    rated_tbw_consumer_ssd: int = 600

    # v0.8.0+ SSD wear ceilings. `wear_pct_used` is 0-100 (0 = factory,
    # 100 = end of rated life). NVMe drives report it directly as
    # `percentage_used`; SATA SSDs report `100 - remaining` via one of
    # attrs 233/177/231/169. Only applies to SSDs — HDDs don't have a
    # wear indicator so this rule is a no-op for them.
    ssd_wear_ceiling_enabled: bool = True
    ssd_wear_a_ceiling_pct: int = 20
    ssd_wear_b_ceiling_pct: int = 50
    ssd_wear_fail_pct: int = 90
    # NVMe-only: if the drive's advertised available_spare_threshold is
    # reached (i.e. `available_spare < threshold`), the firmware is
    # telling the host the drive is near media-exhaustion. Auto-F.
    fail_on_low_nvme_spare: bool = True

    # v0.8.0+ Error-class auto-fail / ceiling rules. Each independently
    # toggleable so operators can soften specific signals without
    # disabling the whole category.
    error_rules_enabled: bool = True
    # SATA attr 184. Internal-integrity check failure. ANY value > 0 is a
    # hard failure signal.
    fail_on_end_to_end_error: bool = True
    # NVMe critical_warning bitfield. Any bit set is a hard failure.
    fail_on_nvme_critical_warning: bool = True
    # NVMe media_errors count > 0 → cap at C (not auto-F by itself
    # because recoverable errors can happen on drives that are otherwise
    # fine; but a drive with any uncorrected media error isn't A-tier).
    cap_c_on_nvme_media_errors: bool = True
    # SATA attr 188 (command timeouts) — cap at B above this count.
    command_timeout_b_ceiling: int = 5
    # Self-test log: cap at C if any past long self-test failed even if
    # current short test passes.
    cap_c_on_past_self_test_failure: bool = True


class FleetConfig(BaseModel):
    """Multi-node fleet config (v0.10.0+).

    DriveForge's default shape is single-node ("standalone"): one daemon
    + one web UI per box, drives attached locally. The fleet feature
    lets a single *operator* node aggregate drives from one or more
    *agent* nodes — useful when someone repurposes a couple of old
    servers as drive-wipe hands, each with limited CPU/RAM that
    couldn't justify its own web-UI instance.

    Three roles:

    - `standalone` (default) — pre-v0.10.0 behavior. No fleet
      endpoints, no enrollment, no remote aggregation. Upgrading from
      v0.9.x leaves this untouched; the fleet feature is opt-in.
    - `operator` — serves the web UI, aggregates drives from any
      enrolled agents, prints cert labels for the whole fleet. Also
      runs its OWN local pipeline (operator == standalone + fleet
      aggregation) so single-box deployments never see the fleet
      concept.
    - `agent` — headless; registers with an operator, reports local
      drives + progress, executes commands the operator sends back.
      No web UI served. Falls back to standalone behavior (just
      without a UI) if the operator is unreachable for a long
      time.

    Fleet transport (v0.10.1+) will be a persistent WebSocket from
    agent → operator on `listen_port`. v0.10.0 only wires the config
    + enrollment + DB plumbing.
    """

    # Role this daemon plays. Default "standalone" means "no fleet
    # features" — same as pre-v0.10.0.
    role: str = "standalone"  # "standalone" | "operator" | "agent"

    # Agent-only: where the operator lives. Accepts http(s)://host:port
    # or ws(s)://host:port — the fleet transport normalizes both.
    # Ignored when role != "agent".
    operator_url: str | None = None

    # Agent-only: filesystem path to the long-lived API token issued
    # at enrollment. Mode 600, owned by the daemon user. Ignored when
    # role != "agent".
    api_token_path: Path = Path("/etc/driveforge/agent.token")

    # Operator-only: TCP port the fleet WebSocket server binds to.
    # Deliberately distinct from the web UI port so firewall rules
    # can scope agent traffic separately from operator-dashboard
    # traffic. Ignored when role != "operator".
    listen_port: int = 8443

    # Human-readable name for this host on the operator's dashboard.
    # Defaults to the system hostname at daemon start if unset.
    # Example: "r720", "nx3200", "xvault-west". Shown on drive-card
    # host badges + the Agents settings page.
    display_name: str | None = None

    # Enrollment-token TTL. Tokens are one-use + short-lived; 15 min
    # is plenty of time for an operator to paste the command on an
    # agent console. Operator generates a fresh token each time via
    # Settings → Agents → "Add agent".
    enrollment_token_ttl_seconds: int = 900


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
    fleet: FleetConfig = Field(default_factory=FleetConfig)

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
