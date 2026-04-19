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
    capacity_bytes: Mapped[int] = mapped_column(Integer, default=0)
    transport: Mapped[str] = mapped_column(String(16), default="unknown")
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


class FirmwareApproval(Base):
    """User-approved (model, transport, version, sha256) firmware entries.

    When auto-apply is enabled, the orchestrator flashes drives whose
    firmware matches an approved entry. Without a row here, entries stay
    check-only regardless of the auto-apply toggle.
    """

    __tablename__ = "firmware_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    transport: Mapped[str] = mapped_column(String(16))
    version: Mapped[str] = mapped_column(String(64))
    blob_sha256: Mapped[str] = mapped_column(String(64))
    signature_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class FirmwareOperation(Base):
    """Audit log of every firmware flash attempt (successful or not).

    One row per drive attempt. Canary operations are flagged so we can
    correlate later same-batch drives back to their canary.
    """

    __tablename__ = "firmware_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    test_run_id: Mapped[int | None] = mapped_column(ForeignKey("test_runs.id"), nullable=True)
    drive_serial: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128))
    from_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_version: Mapped[str] = mapped_column(String(64))
    is_canary: Mapped[bool] = mapped_column(Boolean, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(32))  # success | failed | skipped | deferred
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


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
