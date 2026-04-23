"""SQLAlchemy schema — the canonical local store.

Everything DriveForge knows lives here: drives, batches, test runs per
drive, SMART snapshots (pre/post), telemetry samples, grading rules that
fired, and the outbound-webhook audit log.

This is the source of truth. The webhook payload is a projection of this
DB, never the other way around.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Drive(Base):
    __tablename__ = "drives"

    serial: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128))
    # Normalized manufacturer name ("Intel", "Seagate", etc.). Populated at
    # enrollment via smartctl INQUIRY vendor (SAS) or model-string prefix
    # parse (SATA/NVMe). Displayed on dashboard bay cards.
    manufacturer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capacity_bytes: Mapped[int] = mapped_column(Integer, default=0)
    transport: Mapped[str] = mapped_column(String(16), default="unknown")
    # True = spinning HDD, False = SSD/NVMe, None = unknown (legacy rows).
    # Drives the ETA coefficient pick so SATA/SAS SSDs aren't misclassified as
    # HDDs just because lsblk reports tran=sas on a SAS-HBA-attached SSD.
    rotational: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    firmware_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # v0.10.0+ fleet aggregation. NULL = local to this daemon (operator's
    # own drives OR any standalone install's drives). Non-NULL = the
    # agent this drive was last reported from. Only ever populated on
    # the operator; agents leave this NULL.
    last_host_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    last_host_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    test_runs: Mapped[list["TestRun"]] = relationship(back_populates="drive", cascade="all, delete-orphan")


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    test_runs: Mapped[list["TestRun"]] = relationship(back_populates="batch")


class TestRun(Base):
    __tablename__ = "test_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_serial: Mapped[str] = mapped_column(ForeignKey("drives.serial"), index=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("batches.id"), index=True, nullable=True)
    bay: Mapped[int | None] = mapped_column(Integer, nullable=True)
    phase: Mapped[str] = mapped_column(String(32), default="queued")  # queued|pre_smart|short_test|firmware|erase|badblocks|long_test|post_smart|grading|done|failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(8), nullable=True)
    power_on_hours_at_test: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # End-of-test ("post") SMART counters. Populated at the post-SMART
    # phase. The v0.5.5 pre_* columns below carry the matching start-of-test
    # snapshot so the delta (post - pre) tells the "healing" story — how
    # many pending sectors the drive remapped during our pipeline.
    reallocated_sectors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_pending_sector: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offline_uncorrectable: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Start-of-test snapshot (v0.5.5+). Captured at the pre-SMART phase.
    # NULL on runs that predate v0.5.5 — downstream code treats absent
    # pre-snapshot as "no delta available" rather than as a zero baseline.
    pre_reallocated_sectors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pre_current_pending_sector: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smart_status_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rules: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # grading rationale
    report_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    label_printed: Mapped[bool] = mapped_column(Boolean, default=False)
    quick_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    # Quick-pass triage verdict (v0.5.5+). "clean" | "watch" | "fail".
    # Populated only when quick_mode=True. For full-pipeline runs the
    # grade column carries the verdict instead; triage_result stays NULL.
    #   clean — post_pending=0 AND no climb during run
    #   watch — post_pending>0 AND no climb
    #   fail  — pending or reallocated climbed during the run
    triage_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # v0.5.6+ throughput grading during badblocks. Populated from
    # diskstats samples collected by the orchestrator's throughput
    # sampler while the drive is in the badblocks phase. NULL on:
    #   - quick-pass runs (no badblocks)
    #   - legacy pre-v0.5.6 rows
    #   - runs that failed before entering badblocks
    #   - hosts where diskstats isn't available for this device
    # Per-pass means stored in throughput_pass_means as a JSON array
    # (length equals number of passes completed before pipeline ended;
    # typically 8 for a clean run, fewer if aborted mid-burn-in).
    throughput_mean_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    throughput_p5_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    throughput_p95_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    throughput_pass_means: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)  # last N lines of phase output
    # Phase name at which this run was interrupted by a drive-pull (udev
    # remove while the drive was in active_phase). NULL during normal runs.
    # Set by the hotplug remove handler; cleared or rolled forward by the
    # recover_drive() path when the drive is re-inserted. Presence of a
    # non-NULL value with completed_at NULL marks a run awaiting recovery.
    interrupted_at_phase: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    # v0.6.7+ sanitization method actually used for this run. NULL on
    # quick-pass + legacy pre-v0.6.7 rows + runs that didn't complete
    # the sanitization phase.
    #   "secure_erase"        — ATA SECURITY ERASE UNIT (SAT or hdparm)
    #                            completed normally
    #   "badblocks_overwrite" — secure_erase was unavailable (libata-
    #                            freeze pattern on an HDD), pipeline
    #                            fell through to badblocks' 4-pattern
    #                            destructive write which IS NIST 800-88
    #                            Clear for magnetic media
    #   "none"                — pipeline never reached a sanitization
    #                            step (error during earlier phase)
    # Stamped on the run so the cert label + report can show the honest
    # sanitization method.
    sanitization_method: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # v0.8.0+ lifetime I/O + wear + error-class signals for the
    # buyer-transparency report + new ceiling grading rules. All
    # sourced from the post-SMART snapshot at pipeline finalization.
    # See driveforge.core.smart.SmartSnapshot for field semantics.
    # NULL on pre-v0.8.0 rows and on drives whose transport doesn't
    # report the signal.
    lifetime_host_reads_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lifetime_host_writes_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wear_pct_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_spare_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Error-class counters
    end_to_end_error_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_timeout_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reallocation_event_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NVMe-only
    nvme_critical_warning: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nvme_media_errors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Self-test log summary
    self_test_has_past_failure: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # v0.8.0+ drive-class classification ("enterprise_hdd" / "enterprise_ssd"
    # / "consumer_hdd" / "consumer_ssd"). Captured at finalize time so the
    # grading rationale can reference it honestly + operators can see
    # which rated-TBW bucket the drive fell into.
    drive_class: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # v0.8.0+ Regrade support. Phase="regrade" runs copy historical pipeline
    # results from this source run rather than re-running destructive tests.
    # NULL on original pipeline runs; FK to the sourced TestRun.id for regrade rows.
    regrade_of_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("test_runs.id"), nullable=True, default=None
    )

    # v0.10.0+ fleet aggregation. NULL = run executed on this local
    # daemon (standalone OR operator's own drives). Non-NULL = run
    # executed on a remote agent; value is the agents.id string. Set
    # on the operator when the agent forwards cert + run metadata
    # upstream. Drives the "where was this run executed?" column on
    # the history page.
    host_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    drive: Mapped[Drive] = relationship(back_populates="test_runs")
    batch: Mapped[Batch | None] = relationship(back_populates="test_runs")
    smart_snapshots: Mapped[list["SmartSnapshot"]] = relationship(
        back_populates="test_run", cascade="all, delete-orphan"
    )
    telemetry: Mapped[list["TelemetrySample"]] = relationship(
        back_populates="test_run", cascade="all, delete-orphan"
    )


class SmartSnapshot(Base):
    __tablename__ = "smart_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    test_run_id: Mapped[int] = mapped_column(ForeignKey("test_runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # "pre" | "post" | "interim"
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    test_run: Mapped[TestRun] = relationship(back_populates="smart_snapshots")


class TelemetrySample(Base):
    __tablename__ = "telemetry_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    test_run_id: Mapped[int | None] = mapped_column(ForeignKey("test_runs.id"), index=True, nullable=True)
    drive_serial: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    phase: Mapped[str] = mapped_column(String(32))
    drive_temp_c: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chassis_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    # v0.10.0+ host that produced the sample. NULL = local.
    host_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    test_run: Mapped[TestRun | None] = relationship(back_populates="telemetry")


class Agent(Base):
    """Registered fleet agent (v0.10.0+).

    Present on the *operator* node's DB only. Each row is a remote
    DriveForge daemon that joined this operator's fleet via the
    enrollment flow. The `api_token_hash` is the SHA-256 of the
    bearer token the agent presents on its WebSocket handshake;
    the raw token lives only on the agent side.

    `revoked_at` != NULL means the operator clicked Revoke on this
    agent; further handshake attempts from that token are rejected.
    The row is retained (not deleted) so historical drive/run rows
    with `host_id = agent.id` stay joinable for the drive-detail
    history view.
    """

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64))
    hostname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    api_token_hash: Mapped[str] = mapped_column(String(128))
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional note the operator types on the Agents page (e.g. "rack 2 bench").
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class EnrollmentToken(Base):
    """One-shot enrollment token (v0.10.0+).

    Generated on the operator via Settings → Agents → "Add agent".
    Short-lived (default 15 min via `fleet.enrollment_token_ttl_seconds`),
    single-use. On successful enrollment the row is marked `consumed_at`
    and the resulting agent_id is stored for audit — but the token
    string itself is hashed, never stored in cleartext, so a DB dump
    doesn't leak a stolen-token attack window.
    """

    __tablename__ = "enrollment_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_agent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("batches.id"), nullable=True)
    url: Mapped[str] = mapped_column(String(512))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
