"""Drive cert report generation.

Given a `TestRun`, produce the JSON payload (for API + webhook) and an
HTML view (for the public QR landing page). HTML is rendered via the
daemon's Jinja environment so it can share templates with the web UI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class DriveReport(BaseModel):
    """Self-contained report payload for a single drive test run.

    Used as the body of /reports/<serial> and embedded in the batch-complete
    webhook payload.
    """

    serial: str
    model: str
    capacity_tb: float
    grade: str
    tested_at: datetime
    completed_at: datetime | None
    power_on_hours: int | None
    reallocated_sectors: int | None
    current_pending_sector: int | None
    offline_uncorrectable: int | None
    smart_status_passed: bool | None
    rules: list[dict[str, Any]] = []
    telemetry_summary: dict[str, Any] = {}
    report_url: str
    batch_id: str | None = None
    source: str | None = None
