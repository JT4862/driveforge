#!/usr/bin/env python3
"""Regenerate README screenshots under docs/screenshots/ by driving the
real daemon in dev mode against a seeded dummy DB.

Usage:
    .venv/bin/python scripts/capture-screenshots.py

Prerequisites:
    uv pip install --python .venv/bin/python playwright
    .venv/bin/python -m playwright install chromium

What it does:
  1. Creates a disposable state dir at .driveforge-dev-screenshots/
  2. Initializes the DB there + seeds it with realistic dummy drives,
     batches, test runs, SMART snapshots, and telemetry samples —
     enough variety to exercise every UI state the README shows
     (Active + Installed, multiple grades, F + error, quick-mode,
     recovery-mode, per-drive abort button disabled during
     secure_erase, etc.).
  3. Launches `driveforge-daemon --dev --fixtures tests/fixtures/` as
     a subprocess, pointed at the dummy dir via a temporary
     /etc/driveforge.yaml override (actually we just override
     DRIVEFORGE_DAEMON__STATE_DIR via env var — pydantic-settings
     double-underscore syntax).
  4. Waits for /api/health to return ok.
  5. Uses Playwright Chromium to navigate each target URL and take a
     screenshot, saved to docs/screenshots/{name}.png.
  6. Cleans up the daemon subprocess + temp dir.

Target screenshots:
  dashboard.png       — full drive grid, Active + Installed sections
  drive-detail.png    — rich drive page with SMART, telemetry, log
  report.png          — public cert page at /reports/<serial>
  new-batch.png       — new-batch form with drive selection + quick toggle
  label-preview.png   — drive detail with the label preview modal open

No real hardware is touched by this script. Safe to run on any dev
machine with Playwright + Chromium installed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from driveforge.db import models as m  # noqa: E402
from driveforge.db.session import init_db, make_engine, make_session_factory  # noqa: E402


# ---------------------------------------------------------------- seed


def _smart_payload(
    *,
    reallocated: int = 0,
    pending: int = 0,
    offline_unc: int = 0,
    temp_c: int = 35,
    poh: int = 10_000,
    passed: bool = True,
) -> dict:
    """Build a minimal SMART snapshot JSON payload — the shape the
    orchestrator writes at pre/post phases."""
    return {
        "device": "/dev/sda",
        "captured_at": datetime.now(UTC).isoformat(),
        "power_on_hours": poh,
        "reallocated_sectors": reallocated,
        "current_pending_sector": pending,
        "offline_uncorrectable": offline_unc,
        "smart_status_passed": passed,
        "temperature_c": temp_c,
    }


def seed_database(state_dir: Path) -> None:
    """Populate a fresh dev DB with variety. Drives:

      1. Z1F248SL   ST3000DM001    3 TB SATA  — ACTIVE, badblocks phase
      2. PHWL53230  INTEL SSDSC2BB120G4 120GB SATA SSD — ACTIVE, secure_erase, RECOVERY mode
      3. W1F3XRE9   ST3000DM001    3 TB SATA  — INSTALLED, Grade A (today)
      4. S0K234QH   ST300MM0006    300 GB SAS — INSTALLED, Grade A quick-mode*
      5. WD-WMC1    WDC WD20EFRX   2 TB SATA  — INSTALLED, Grade B (yesterday)
      6. HUS-VAL1   HUS724030ALA640 3 TB SAS  — INSTALLED, Grade C (older)
      7. ST-FAIL1   ST2000NM0045   2 TB SAS   — INSTALLED, Grade F (reallocated over threshold)
      8. SSD-ERR1   SAMSUNG 860EVO 500GB SATA — INSTALLED, ERR (pipeline error)
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "driveforge.db"
    if db_path.exists():
        db_path.unlink()

    engine = make_engine(db_path)
    init_db(engine)
    Session = make_session_factory(engine)

    now = datetime.now(UTC)

    with Session() as session:
        # Six drives, one per Installed-section card. Matches the
        # custom lsblk fixture in SCREENSHOT_LSBLK_FIXTURE. Each has
        # a completed TestRun with a distinct grade to show the full
        # range of grade states the dashboard can render.
        drives_data = [
            # serial, model, manufacturer, capacity, transport, firmware, rotational
            ("W1F3XRE9", "ST3000DM001-1CH166", "Seagate", 3_000_592_982_016, "sata", "CC26", True),       # A
            ("S0K234QH", "ST300MM0006", "Seagate", 300_000_000_000, "sas", "LS08", True),                 # A quick*
            ("WD-WMC1T0H5AAAA", "WDC WD20EFRX-68EUZN0", "Western Digital", 2_000_398_934_016, "sata", "82.00A82", True),  # B
            ("HUS724030ALA640", "HUS724030ALA640", "HGST", 3_000_592_982_016, "sas", "A5C0", True),       # C
            ("ST2000NM0045-BAD", "ST2000NM0045-1V4204", "Seagate", 2_000_398_934_016, "sas", "TN02", True),  # F
            ("SAMSUNG-ERR-EVO1", "Samsung SSD 860 EVO 500GB", "Samsung", 500_107_862_016, "sata", "RVT03B6Q", False),  # ERR
        ]
        for serial, model, mfr, cap, transport, fw, rota in drives_data:
            session.add(m.Drive(
                serial=serial,
                model=model,
                manufacturer=mfr,
                capacity_bytes=cap,
                transport=transport,
                firmware_version=fw,
                rotational=rota,
                first_seen_at=now - timedelta(days=30),
            ))

        # ---------- Completed batch from earlier today ----------
        batch_today = m.Batch(
            id="b20260421morn",
            source="auto-enroll (quick)",
            started_at=now - timedelta(hours=4),
            completed_at=now - timedelta(hours=1, minutes=15),
        )
        session.add(batch_today)

        batch_yesterday = m.Batch(
            id="b20260420eve",
            source="web ui",
            started_at=now - timedelta(days=1, hours=6),
            completed_at=now - timedelta(days=1, hours=1),
        )
        session.add(batch_yesterday)

        batch_older = m.Batch(
            id="b20260418wkd",
            source="web ui",
            started_at=now - timedelta(days=3, hours=8),
            completed_at=now - timedelta(days=3, hours=2),
        )
        session.add(batch_older)

        # ---------- Completed TestRuns for installed drives ----------
        runs: list[m.TestRun] = []

        # Grade A — drive 3 (W1F3XRE9) from this morning, full mode
        r3 = m.TestRun(
            drive_serial="W1F3XRE9",
            batch_id="b20260421morn",
            phase="done",
            started_at=now - timedelta(hours=4),
            completed_at=now - timedelta(hours=1, minutes=30),
            grade="A",
            quick_mode=False,
            power_on_hours_at_test=12_432,
            reallocated_sectors=0,
            current_pending_sector=0,
            offline_uncorrectable=0,
            smart_status_passed=True,
            rules=[
                {"name": "smart_short_test_passed", "passed": True, "detail": "SMART short self-test passed", "forces_grade": None},
                {"name": "smart_long_test_passed", "passed": True, "detail": "SMART long self-test passed", "forces_grade": None},
                {"name": "badblocks_clean", "passed": True, "detail": "badblocks reported no errors", "forces_grade": None},
                {"name": "no_pending_sectors", "passed": True, "detail": "current_pending_sector=0", "forces_grade": None},
                {"name": "no_offline_uncorrectable", "passed": True, "detail": "offline_uncorrectable=0", "forces_grade": None},
                {"name": "no_degradation_reallocated_sectors", "passed": True, "detail": "reallocated_sectors: pre=0 → post=0", "forces_grade": None},
                {"name": "grade_a_reallocated", "passed": True, "detail": "reallocated_sectors=0 ≤ 3 (A)", "forces_grade": None},
            ],
            report_url="/reports/W1F3XRE9",
        )
        runs.append(r3)

        # Grade A quick-mode — drive 4 (S0K234QH), yesterday
        r4 = m.TestRun(
            drive_serial="S0K234QH",
            batch_id="b20260420eve",
            phase="done",
            started_at=now - timedelta(days=1, hours=6),
            completed_at=now - timedelta(days=1, hours=5, minutes=15),
            grade="A",
            quick_mode=True,
            power_on_hours_at_test=8_932,
            reallocated_sectors=0,
            current_pending_sector=0,
            offline_uncorrectable=0,
            smart_status_passed=True,
            rules=[
                {"name": "smart_short_test_passed", "passed": True, "detail": "SMART short self-test passed", "forces_grade": None},
                {"name": "badblocks_clean", "passed": True, "detail": "badblocks skipped (quick mode)", "forces_grade": None},
                {"name": "grade_a_reallocated", "passed": True, "detail": "reallocated_sectors=0 ≤ 3 (A)", "forces_grade": None},
            ],
            report_url="/reports/S0K234QH",
        )
        runs.append(r4)

        # Grade B — drive 5 (WD-WMC1T0H5AAAA), yesterday
        r5 = m.TestRun(
            drive_serial="WD-WMC1T0H5AAAA",
            batch_id="b20260420eve",
            phase="done",
            started_at=now - timedelta(days=1, hours=6),
            completed_at=now - timedelta(days=1, hours=2),
            grade="B",
            quick_mode=False,
            power_on_hours_at_test=28_614,
            reallocated_sectors=6,
            current_pending_sector=0,
            offline_uncorrectable=0,
            smart_status_passed=True,
            rules=[
                {"name": "smart_short_test_passed", "passed": True, "detail": "SMART short self-test passed", "forces_grade": None},
                {"name": "smart_long_test_passed", "passed": True, "detail": "SMART long self-test passed", "forces_grade": None},
                {"name": "badblocks_clean", "passed": True, "detail": "badblocks reported no errors", "forces_grade": None},
                {"name": "grade_b_reallocated", "passed": True, "detail": "reallocated_sectors=6 ≤ 8 (B)", "forces_grade": None},
            ],
            report_url="/reports/WD-WMC1T0H5AAAA",
        )
        runs.append(r5)

        # Grade C — drive 6 (HUS724030ALA640), 3 days ago
        r6 = m.TestRun(
            drive_serial="HUS724030ALA640",
            batch_id="b20260418wkd",
            phase="done",
            started_at=now - timedelta(days=3, hours=8),
            completed_at=now - timedelta(days=3, hours=3),
            grade="C",
            quick_mode=False,
            power_on_hours_at_test=56_211,
            reallocated_sectors=32,
            current_pending_sector=0,
            offline_uncorrectable=0,
            smart_status_passed=True,
            rules=[
                {"name": "smart_short_test_passed", "passed": True, "detail": "SMART short self-test passed", "forces_grade": None},
                {"name": "smart_long_test_passed", "passed": True, "detail": "SMART long self-test passed", "forces_grade": None},
                {"name": "badblocks_clean", "passed": True, "detail": "badblocks reported no errors", "forces_grade": None},
                {"name": "grade_c_reallocated", "passed": True, "detail": "reallocated_sectors=32 ≤ 40 (C)", "forces_grade": None},
            ],
            report_url="/reports/HUS724030ALA640",
        )
        runs.append(r6)

        # Grade F — drive 7 (ST2000NM0045-BAD), today
        r7 = m.TestRun(
            drive_serial="ST2000NM0045-BAD",
            batch_id="b20260421morn",
            phase="failed",
            started_at=now - timedelta(hours=4),
            completed_at=now - timedelta(hours=2, minutes=45),
            grade="F",
            quick_mode=False,
            power_on_hours_at_test=52_341,
            reallocated_sectors=47,
            current_pending_sector=3,
            offline_uncorrectable=0,
            smart_status_passed=True,
            rules=[
                {"name": "smart_short_test_passed", "passed": True, "detail": "SMART short self-test passed", "forces_grade": None},
                {"name": "badblocks_clean", "passed": True, "detail": "badblocks reported no errors", "forces_grade": None},
                {"name": "no_pending_sectors", "passed": False, "detail": "current_pending_sector=3", "forces_grade": "F"},
                {"name": "grade_c_reallocated", "passed": False, "detail": "reallocated_sectors=47 > 40 (fail)", "forces_grade": "F"},
            ],
            error_message="[grading] current_pending_sector=3 (> 0); reallocated_sectors=47 (> 40 threshold)",
            report_url="/reports/ST2000NM0045-BAD",
        )
        runs.append(r7)

        # Pipeline ERROR — drive 8 (SAMSUNG-ERR-EVO1), today
        r8 = m.TestRun(
            drive_serial="SAMSUNG-ERR-EVO1",
            batch_id="b20260421morn",
            phase="failed",
            started_at=now - timedelta(hours=4),
            completed_at=now - timedelta(hours=3, minutes=50),
            grade="error",
            quick_mode=False,
            error_message="[secure_erase] SAT passthrough SECURITY ERASE UNIT failed: sg_raw timed out after 21600s",
        )
        runs.append(r8)

        for run in runs:
            session.add(run)
        session.flush()

        # ---------- SMART snapshots for completed runs + active drive 1 ----------
        for run in runs:
            if run.grade in ("A", "B", "C", "F"):
                session.add(m.SmartSnapshot(
                    test_run_id=run.id,
                    kind="pre",
                    captured_at=run.started_at,
                    payload=_smart_payload(
                        reallocated=run.reallocated_sectors or 0,
                        pending=run.current_pending_sector or 0,
                        offline_unc=run.offline_uncorrectable or 0,
                        temp_c=34,
                        poh=(run.power_on_hours_at_test or 0) - 1,
                        passed=bool(run.smart_status_passed),
                    ),
                ))
                session.add(m.SmartSnapshot(
                    test_run_id=run.id,
                    kind="post",
                    captured_at=run.completed_at,
                    payload=_smart_payload(
                        reallocated=run.reallocated_sectors or 0,
                        pending=run.current_pending_sector or 0,
                        offline_unc=run.offline_uncorrectable or 0,
                        temp_c=41,
                        poh=run.power_on_hours_at_test or 0,
                        passed=bool(run.smart_status_passed),
                    ),
                ))

        # ---------- Telemetry samples for r3 (pass-tier detail page) ----------
        # A realistic-looking temperature curve over the 2.5h run
        for i in range(30):
            t = r3.started_at + timedelta(minutes=5 * i)
            # Temp: starts cool, warms during badblocks, cools off after
            if i < 6:
                temp = 34 + i  # warming up
            elif i < 22:
                temp = 40 + (i % 5) - 2  # badblocks — hovering 38-43
            else:
                temp = 41 - (i - 21)  # cooling down
            temp = max(30, min(50, temp))
            power = 168 + (i % 7) - 3  # ~165-172 W
            session.add(m.TelemetrySample(
                test_run_id=r3.id,
                drive_serial=r3.drive_serial,
                ts=t,
                phase="badblocks" if 6 <= i < 22 else ("secure_erase" if i < 6 else "long_test"),
                drive_temp_c=temp,
                chassis_power_w=float(power),
            ))

        session.commit()
        print(f"seeded DB at {db_path}: {len(drives_data)} drives, {len(runs)} runs, 3 batches")


# ---------------------------------------------------------------- capture


SCREENSHOT_TARGETS = [
    # (name, path, viewport_wait)
    ("dashboard", "/", 2000),
    ("drive-detail", "/drives/W1F3XRE9", 2500),
    ("report", "/reports/W1F3XRE9", 2000),
    ("new-batch", "/batches/new", 1500),
    # label-preview is captured separately — needs to click a button to open the modal
]


def capture_screenshots(base_url: str, output_dir: Path) -> None:
    """Use Playwright Chromium to capture the target URLs."""
    from playwright.sync_api import sync_playwright

    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # 1400x900 is wide enough for the dashboard to show chassis strip
        # comfortably without horizontal overflow; height gets cropped to
        # content per screenshot. Device scale 2x for crisp README images
        # on HiDPI displays.
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            device_scale_factor=2,
        )
        page = context.new_page()

        for name, path, wait_ms in SCREENSHOT_TARGETS:
            url = base_url.rstrip("/") + path
            print(f"→ {name}: {url}")
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(wait_ms)
            target = output_dir / f"{name}.png"
            page.screenshot(path=str(target), full_page=True)
            print(f"  saved {target}")

        # Label-preview modal — navigate to a Grade A drive's detail
        # page, click the "Preview cert label" button, wait for the
        # modal to render the label PNG, screenshot the modal region.
        label_url = base_url.rstrip("/") + "/drives/W1F3XRE9"
        print(f"→ label-preview: {label_url} (with modal open)")
        page.goto(label_url, wait_until="networkidle")
        page.wait_for_timeout(1000)
        # Click the Preview button (the modal is opened via showModal())
        page.click("text=Preview cert label")
        # Wait for the modal image to load
        page.wait_for_timeout(2500)
        # Screenshot the dialog specifically — it's a <dialog> element with id.
        # Fall back to full-page if dialog targeting fails.
        target = output_dir / "label-preview.png"
        try:
            dialog = page.locator("#label-preview-modal")
            dialog.screenshot(path=str(target))
        except Exception as exc:
            print(f"  dialog-targeted screenshot failed ({exc}); falling back to page")
            page.screenshot(path=str(target))
        print(f"  saved {target}")

        browser.close()


# ---------------------------------------------------------------- daemon mgmt


def wait_for_daemon(base_url: str, timeout_s: int = 30) -> bool:
    """Poll /api/health until it returns 200 OK or timeout."""
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(base_url + "/api/health", timeout=2)
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


SCREENSHOT_LSBLK_FIXTURE = json.dumps({
    "blockdevices": [
        # Six drives to show across the Installed section. Matches the
        # serials seeded by `seed_database` so the dashboard's
        # discover() → state.active_phase.keys() diff shows each drive
        # with its real grade from the DB.
        {"name": "sda", "model": "ST3000DM001-1CH166", "serial": "W1F3XRE9", "size": 3_000_592_982_016, "tran": "sata", "rota": True, "type": "disk", "rev": "CC26"},
        {"name": "sdb", "model": "ST300MM0006", "serial": "S0K234QH", "size": 300_000_000_000, "tran": "sas", "rota": True, "type": "disk", "rev": "LS08"},
        {"name": "sdc", "model": "WDC WD20EFRX-68EUZN0", "serial": "WD-WMC1T0H5AAAA", "size": 2_000_398_934_016, "tran": "sata", "rota": True, "type": "disk", "rev": "82.00A82"},
        {"name": "sdd", "model": "HUS724030ALA640", "serial": "HUS724030ALA640", "size": 3_000_592_982_016, "tran": "sas", "rota": True, "type": "disk", "rev": "A5C0"},
        {"name": "sde", "model": "ST2000NM0045-1V4204", "serial": "ST2000NM0045-BAD", "size": 2_000_398_934_016, "tran": "sas", "rota": True, "type": "disk", "rev": "TN02"},
        {"name": "sdf", "model": "Samsung SSD 860 EVO 500GB", "serial": "SAMSUNG-ERR-EVO1", "size": 500_107_862_016, "tran": "sata", "rota": False, "type": "disk", "rev": "RVT03B6Q"},
    ]
}, indent=2)


def main() -> int:
    # The daemon's `--fixtures` flag hardcodes `cwd/.driveforge-dev`
    # as the state dir, overriding any env-var setting. Rather than
    # fighting that, we use the same path for our seed → the daemon
    # reads the DB we just wrote.
    state_dir = REPO_ROOT / ".driveforge-dev"
    # Custom fixtures dir — copy the committed tests/fixtures and
    # overwrite lsblk with our seed-matching content. Daemon reads
    # from here via --fixtures.
    fixtures_dir = state_dir / "fixtures"
    output_dir = REPO_ROOT / "docs" / "screenshots"

    # Clear any prior dev state so the seed is deterministic.
    if state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True)

    # Copy the committed test fixtures tree (smartctl, nvme-cli, ipmitool
    # outputs etc.), then override the lsblk fixture to match our seed.
    shutil.copytree(REPO_ROOT / "tests" / "fixtures", fixtures_dir)
    lsblk_fixture = fixtures_dir / "lsblk" / "_default.stdout"
    lsblk_fixture.parent.mkdir(parents=True, exist_ok=True)
    lsblk_fixture.write_text(SCREENSHOT_LSBLK_FIXTURE)
    print(f"wrote custom lsblk fixture with {len(json.loads(SCREENSHOT_LSBLK_FIXTURE)['blockdevices'])} drives")

    print("==> Seeding dummy DB")
    seed_database(state_dir)

    # Skip the wizard gate so /setup redirect doesn't intercept us.
    env = {
        **os.environ,
        "DRIVEFORGE_SETUP_COMPLETED": "true",
    }

    print("==> Launching daemon on 127.0.0.1:18080")
    daemon_proc = subprocess.Popen(
        [
            str(REPO_ROOT / ".venv/bin/driveforge-daemon"),
            "--dev",
            "--fixtures", str(fixtures_dir),
            "--host", "127.0.0.1",
            "--port", "18080",
        ],
        env=env,
        cwd=str(REPO_ROOT),  # ensure .driveforge-dev resolves to our seed dir
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = "http://127.0.0.1:18080"

    try:
        if not wait_for_daemon(base_url, timeout_s=30):
            print("✗ daemon failed to come up within 30s")
            daemon_proc.send_signal(signal.SIGTERM)
            out, _ = daemon_proc.communicate(timeout=5)
            print(out.decode(errors="replace"))
            return 1
        print("✓ daemon ready")

        print("==> Capturing screenshots")
        capture_screenshots(base_url, output_dir)
    finally:
        print("==> Shutting down daemon")
        daemon_proc.send_signal(signal.SIGTERM)
        try:
            daemon_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon_proc.kill()

    print(f"✓ screenshots written to {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
