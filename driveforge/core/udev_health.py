"""udev-queue / systemd-udevd health detection.

v0.6.9+. Watches for the failure mode observed on R720 + NX-3200
during v0.6.x validation: a D-state drive subprocess (sg_raw,
smartctl, hdparm) behind the HBA SG queue can cascade upward and
wedge the HBA firmware-event kthread, which in turn stalls
systemd-udevd's worker pool. When that happens, newly-inserted
drives get a /dev node (or sometimes don't) but udev never completes
its add-event processing, so pyudev's hotplug monitor never
delivers the event and DriveForge never sees the drive.

This module provides the *detection* half (a pure Python function
that returns a `UdevHealth` snapshot) and the *mitigation* half (a
polkit-authorized trigger that starts
`driveforge-udev-restart.service`, which runs `systemctl restart
systemd-udevd` as root). The UI wires them together: when detection
fires, the dashboard surfaces a one-click "Restart udev" button.

Restarting systemd-udevd is safe — it does not disturb already-
mounted filesystems or in-flight pipelines. Fresh worker processes
spawn, drain any backlog, and new hotplug events process normally.
Drives currently in long-running sg_raw / smartctl calls are
unaffected because those are DriveForge's own child processes, not
udev workers.

Design notes:
  - Detection is polling-based. There is no inotify / netlink hook
    for "udev queue is stuck"; we have to sample. 60s cadence is the
    compromise between "caught fast enough to matter" and "not
    constantly shelling out to udevadm".
  - The three signals are combined into one ternary state
    (OK / DEGRADED / STALLED). Only STALLED surfaces an operator-
    facing banner; DEGRADED just logs.
  - We do NOT auto-restart on STALLED. The human is in the loop:
    the banner has a button, the button hits polkit, and the
    operator is the one who consented. Auto-restart would race with
    ongoing drive subprocess ownership in ways that are hard to
    reason about.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class UdevHealthState(str, Enum):
    """Three-state health summary.

    OK        — settle returns quickly, no D-state udev-workers, no
                orphan block devices in sysfs. Steady state.
    DEGRADED  — one signal is off but the queue is still draining
                (settle takes longer than normal, or one worker is
                transiently Ds). Informational; no operator action.
    STALLED   — settle fails AND at least one orphan block device
                (a /sys/block/sdX with empty model/serial, i.e. udev
                never finished running its rules). Operator-visible;
                the dashboard surfaces the restart button.
    """

    OK = "ok"
    DEGRADED = "degraded"
    STALLED = "stalled"


@dataclass
class UdevHealth:
    """Snapshot of udev-pipeline health. Produced by `check()`.

    Kept serialization-friendly (no process handles etc.) so it can
    be cached in `DaemonState.udev_health` and rendered into Jinja
    globals for templates + the `/api/udev/status` endpoint.
    """

    state: UdevHealthState
    settle_ok: bool
    settle_elapsed_s: float
    udevd_dstate_count: int  # systemd-udevd processes in D/Ds state
    orphan_block_devices: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    note: str = ""  # human-readable one-liner for the dashboard banner

    @property
    def needs_operator_action(self) -> bool:
        """True iff the dashboard should surface the 'Restart udev'
        banner. Only STALLED qualifies — DEGRADED is informational."""
        return self.state == UdevHealthState.STALLED


# ------------------------------------------------------------------- settle


def _run_udevadm_settle(timeout_s: float = 2.0) -> tuple[bool, float]:
    """Run `udevadm settle --timeout=<N>` and return (ok, elapsed_s).

    `udevadm settle` blocks until the udev event queue is drained or
    the timeout expires. Exit code 0 = queue drained; non-zero = still
    events pending after timeout, i.e. udev isn't keeping up (or is
    wedged). We use a short timeout (2s) because a healthy queue
    drains in milliseconds; we're testing for "stuck", not "slow".
    """
    started = datetime.now(UTC)
    try:
        result = subprocess.run(
            ["udevadm", "settle", f"--timeout={int(timeout_s)}"],
            capture_output=True,
            timeout=timeout_s + 3,  # outer wall-clock guard vs. subprocess itself hanging
            check=False,
        )
        elapsed = (datetime.now(UTC) - started).total_seconds()
        return (result.returncode == 0, elapsed)
    except FileNotFoundError:
        # udevadm not on PATH — non-Debian or minimal container. Treat
        # as OK (can't meaningfully check) to avoid false alarms on
        # dev boxes.
        return (True, 0.0)
    except subprocess.TimeoutExpired:
        elapsed = (datetime.now(UTC) - started).total_seconds()
        return (False, elapsed)


# ---------------------------------------------------------- process scanning


def _count_dstate_udevd_processes(proc_root: Path = Path("/proc")) -> int:
    """Count systemd-udevd processes (main + workers) in D/Ds state.

    Reads /proc/<pid>/stat + /proc/<pid>/comm. A healthy udev pool has
    its workers in S (sleeping) waiting for events; D (uninterruptible
    sleep) means a worker is stuck on a syscall that won't return,
    which is exactly the pileup this module watches for.

    Defaults to scanning /proc; overridable for tests.
    """
    count = 0
    if not proc_root.is_dir():
        return 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if comm != "systemd-udevd":
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # /proc/<pid>/stat format: "<pid> (comm) <state> ..."  The comm
        # itself may contain spaces/parens, so find the LAST ')' and
        # take the next whitespace-delimited token.
        rparen = stat.rfind(")")
        if rparen == -1:
            continue
        rest = stat[rparen + 1 :].strip()
        tokens = rest.split(None, 1)
        if not tokens:
            continue
        state_char = tokens[0]
        if state_char in ("D",):
            count += 1
    return count


# ------------------------------------------------------------ sysfs orphans


def _find_orphan_block_devices(sysfs_block: Path = Path("/sys/block")) -> list[str]:
    """Find /sys/block/sd* entries whose udev-populated attributes are
    missing or empty.

    When udev completes its add-event rules for a SCSI/SATA disk it
    populates `/sys/block/sdX/device/model` and `.../vendor`. If those
    files exist but are empty (or if `device/` itself is missing on a
    normally-present disk), the drive was discovered by the kernel but
    udev's rule pipeline didn't complete — the "pyudev event never
    fires" symptom.

    We only consider sd[a-z] (+ multi-letter variants for >26 disks)
    to avoid false-positives on loop/ram/md devices.

    Returns basenames (e.g. ["sdc", "sdd"]) for the banner + log.
    """
    if not sysfs_block.is_dir():
        return []
    orphans: list[str] = []
    for entry in sorted(sysfs_block.iterdir()):
        name = entry.name
        if not (name.startswith("sd") and len(name) >= 3 and name[2:].isalpha()):
            continue
        device_dir = entry / "device"
        if not device_dir.is_dir():
            # No device/ at all — could be a partition-only view; skip.
            continue
        model_file = device_dir / "model"
        vendor_file = device_dir / "vendor"
        try:
            model = model_file.read_text().strip() if model_file.exists() else ""
            vendor = vendor_file.read_text().strip() if vendor_file.exists() else ""
        except OSError:
            continue
        # Both empty = udev never ran ata_id/scsi_id/etc. to populate.
        # Note: some storage stacks legitimately have empty vendor on
        # NVMe; we guard by requiring BOTH empty on sd* specifically.
        if not model and not vendor:
            orphans.append(name)
    return orphans


# ----------------------------------------------------------- public detector


def check(
    *,
    settle_timeout_s: float = 2.0,
    proc_root: Path = Path("/proc"),
    sysfs_block: Path = Path("/sys/block"),
) -> UdevHealth:
    """Run all three probes and return a `UdevHealth` snapshot.

    Classification logic:
      - settle OK + no Ds workers + no orphans → OK
      - settle fails AND orphan(s) present → STALLED
      - any other mismatch → DEGRADED (logged, not surfaced)
    """
    settle_ok, settle_elapsed = _run_udevadm_settle(settle_timeout_s)
    dstate = _count_dstate_udevd_processes(proc_root)
    orphans = _find_orphan_block_devices(sysfs_block)

    if settle_ok and dstate == 0 and not orphans:
        state = UdevHealthState.OK
        note = ""
    elif not settle_ok and orphans:
        state = UdevHealthState.STALLED
        if len(orphans) == 1:
            note = (
                f"udev pipeline is stuck — drive {orphans[0]} is visible to the "
                f"kernel but udev never finished discovering it. A udev restart "
                f"(safe; doesn't disturb in-flight pipelines) will unblock new "
                f"insertions."
            )
        else:
            sample = ", ".join(orphans[:3])
            more = "" if len(orphans) <= 3 else f" (+{len(orphans) - 3} more)"
            note = (
                f"udev pipeline is stuck — {len(orphans)} drives ({sample}{more}) "
                f"are visible to the kernel but udev never finished discovering "
                f"them. A udev restart (safe; doesn't disturb in-flight pipelines) "
                f"will unblock new insertions."
            )
    else:
        state = UdevHealthState.DEGRADED
        bits = []
        if not settle_ok:
            bits.append(f"settle timed out in {settle_elapsed:.1f}s")
        if dstate:
            bits.append(f"{dstate} udev worker(s) in D-state")
        if orphans:
            bits.append(f"{len(orphans)} orphan sysfs block device(s)")
        note = "; ".join(bits) if bits else ""

    health = UdevHealth(
        state=state,
        settle_ok=settle_ok,
        settle_elapsed_s=settle_elapsed,
        udevd_dstate_count=dstate,
        orphan_block_devices=orphans,
        note=note,
    )

    if state == UdevHealthState.STALLED:
        logger.warning("udev health: STALLED — %s", note)
    elif state == UdevHealthState.DEGRADED:
        logger.info("udev health: degraded — %s", note)

    return health


# ------------------------------------------------------------- restart path


def trigger_udev_restart() -> tuple[bool, str]:
    """Request a `systemctl restart systemd-udevd` via the polkit-
    authorized `driveforge-udev-restart.service`.

    Same pattern as `updates.trigger_in_app_update` — the daemon user
    (`driveforge`) doesn't have direct permission to restart
    systemd-udevd, but the polkit rule at
    `/etc/polkit-1/rules.d/51-driveforge-udev-restart.rules` whitelists
    `StartUnit` on this specific service. The service itself is a
    oneshot that runs `systemctl restart systemd-udevd`.

    `--no-block` matters for the same reason it did for the update
    trigger: the caller (HTTP handler) has a short timeout; the unit's
    ExecStart takes a few seconds as udev respawns. Without
    --no-block, `systemctl start` would wait for completion and the
    HTTP handler might time out.

    Returns (ok, detail):
      ok=True means systemctl accepted the StartUnit request. It does
      NOT mean the restart completed — that's asynchronous. The next
      `check()` call will confirm via the OK state.
    """
    argv = [
        "systemctl",
        "start",
        "--no-block",
        "driveforge-udev-restart.service",
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (False, "systemctl start timed out (polkit rule may be missing)")
    except FileNotFoundError:
        return (False, "systemctl not on PATH")

    if result.returncode == 0:
        return (True, "udev restart requested")

    stderr = (result.stderr or "").strip()
    if "interactive authentication required" in stderr.lower():
        return (
            False,
            "polkit rule for driveforge-udev-restart.service is missing or "
            "mis-installed (interactive auth required)",
        )
    return (False, f"systemctl start failed (rc={result.returncode}): {stderr}")


# ------------------------------------------------------ helpers for tests / __main__


def _format_cli(health: UdevHealth) -> str:
    """Pretty-print for a potential `driveforge udev-status` CLI later.
    Not currently wired into the CLI but kept alongside the module so
    the formatting lives next to the data."""
    bits = [
        f"state: {health.state.value}",
        f"settle: {'ok' if health.settle_ok else 'FAILED'} ({health.settle_elapsed_s:.2f}s)",
        f"udevd D-state workers: {health.udevd_dstate_count}",
        f"orphan block devices: {', '.join(health.orphan_block_devices) or 'none'}",
    ]
    if health.note:
        bits.append(f"note: {health.note}")
    return "\n".join(bits)


if __name__ == "__main__":  # pragma: no cover
    # Manual probe — `python -m driveforge.core.udev_health`
    h = check()
    print(_format_cli(h))
    raise SystemExit(0 if h.state == UdevHealthState.OK else 1)
