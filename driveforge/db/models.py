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
    reallocated_sectors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_pending_sector: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offline_uncorrectable: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smart_status_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rules: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # grading rationale
    report_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    label_printed: Mapped[bool] = mapped_column(Boolean, default=False)
    quick_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)  # last N lines of phase output
    # Phase name at which this run was interrupted by a drive-pull (udev
    # remove while the drive was in active_phase). NULL during normal runs.
    # Set by the hotplug remove handler; cleared or rolled forward by the
    # recover_drive() path when the drive is re-inserted. Presence of a
    # non-NULL value with completed_at NULL marks a run awaiting recovery.
    interrupted_at_phase: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)

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

    test_run: Mapped[TestRun | None] = relationship(back_populates="telemetry")


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
