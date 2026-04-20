"""Live per-drive I/O rate via `/proc/diskstats`.

`/proc/diskstats` exposes monotonic per-block-device counters that the
kernel bumps on every I/O. Sampling twice, N seconds apart, and diffing
gives instantaneous throughput — useful on the dashboard during badblocks
and secure-erase phases where the subprocess emits no native progress
signal beyond the big "pass 3/8" sweep markers. Seeing "138 MB/s write"
vs "0 MB/s" at a glance is the quickest "is this drive actually doing
something or has it wedged?" check we can offer.

The format of `/proc/diskstats` is:
    <major> <minor> <name> <rd_ios> <rd_merges> <rd_sectors> <rd_ms>
    <wr_ios> <wr_merges> <wr_sectors> <wr_ms> <in_flight> <io_ms>
    <weighted_io_ms> [<discard_fields>...]

Kernel 4.18+ adds discard/flush fields; we ignore everything past wr_ms.
A "sector" here is always 512 bytes regardless of the drive's actual
physical sector size — that's a kernel convention, not a drive property.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

SECTOR_BYTES = 512
DISKSTATS_PATH = Path("/proc/diskstats")


@dataclass(frozen=True)
class DiskCounter:
    """Snapshot of one device's cumulative I/O counters."""
    name: str          # block device basename, e.g. "sda"
    rd_sectors: int    # cumulative 512-byte sectors read
    wr_sectors: int    # cumulative 512-byte sectors written


def parse_diskstats(text: str) -> dict[str, DiskCounter]:
    """Parse the full `/proc/diskstats` text into {name: counter}.

    Skips partition entries (sda1, sdb2, nvme0n1p1, ...) and keeps only
    whole devices — a drive's partitions double-count against the parent
    and would inflate the reported rate. Heuristic: if the name ends in
    a digit AND a same-prefixed entry without the digit also exists, it's
    a partition. Simpler: filter names matching the common partition
    suffixes we see in practice (`sdXN`, `nvmeXnNpM`, `mmcblkNpM`).
    """
    counters: dict[str, DiskCounter] = {}
    for raw_line in text.splitlines():
        parts = raw_line.split()
        # Need at least 11 columns through wr_ms for a useful record.
        if len(parts) < 11:
            continue
        name = parts[2]
        if _is_partition(name):
            continue
        try:
            counters[name] = DiskCounter(
                name=name,
                rd_sectors=int(parts[5]),
                wr_sectors=int(parts[9]),
            )
        except ValueError:
            continue
    return counters


def _is_partition(name: str) -> bool:
    """Best-effort: does this diskstats name refer to a partition?

    - `sda1`, `sdb12` → partition (digit suffix on a 2-3 char sdX base)
    - `nvme0n1p1`    → partition (p<digit> suffix after nvmeXnY)
    - `mmcblk0p1`    → partition (p<digit> suffix)
    - `sda`, `nvme0n1`, `sr0`, `zd0`, `dm-0` → whole device
    """
    if "p" in name and (name.startswith("nvme") or name.startswith("mmcblk")):
        # nvme0n1p1 → True; nvme0n1 → False (no 'p' after n1 part)
        tail = name.rsplit("p", 1)[-1]
        return tail.isdigit()
    if name.startswith("sd") and len(name) > 3 and name[-1].isdigit():
        return True
    if name.startswith("hd") and len(name) > 3 and name[-1].isdigit():
        return True
    return False


def read_diskstats() -> dict[str, DiskCounter]:
    """Read and parse the current `/proc/diskstats`. Empty on non-Linux."""
    try:
        return parse_diskstats(DISKSTATS_PATH.read_text())
    except FileNotFoundError:
        # Dev laptop (macOS) has no /proc. Return empty so the dashboard
        # just omits rate rather than erroring.
        return {}
    except OSError:
        return {}


@dataclass
class IoRate:
    """Instantaneous I/O rate for one device, MB/s over the last sample window."""
    read_mbps: float
    write_mbps: float


class IoRateTracker:
    """Samples `/proc/diskstats` on demand and returns per-device MB/s.

    Holds one previous snapshot; each `poll()` diffs against it and then
    replaces it. A 1-5 second polling interval is the sweet spot — shorter
    than that and rounding dominates, longer and a momentary stall goes
    unseen. The orchestrator runs this at 3 s in the lifespan task.

    Not thread-safe; all calls happen on the asyncio event loop.
    """

    def __init__(self) -> None:
        self._last_counters: dict[str, DiskCounter] = {}
        # None means "no baseline yet" — must NOT be 0.0, because monotonic
        # clocks legitimately report 0.0 as a valid first reading in tests
        # (and in principle on some platforms right after boot).
        self._last_ts: float | None = None

    def poll(self) -> dict[str, IoRate]:
        """Return {device_name: IoRate} based on delta since last poll.

        First call returns empty (no baseline to diff against). Unchanged
        counters return 0.0/0.0 rather than being omitted — a drive that
        just went idle should show 0 MB/s, not disappear from the map.
        """
        now_ts = time.monotonic()
        current = read_diskstats()
        if self._last_ts is None or not self._last_counters:
            # Bootstrap: record baseline and return empty.
            self._last_counters = current
            self._last_ts = now_ts
            return {}
        dt = now_ts - self._last_ts
        if dt <= 0:
            # Clock didn't advance — skip this tick.
            return {}
        rates: dict[str, IoRate] = {}
        for name, cur in current.items():
            prev = self._last_counters.get(name)
            if prev is None:
                continue
            d_rd = max(0, cur.rd_sectors - prev.rd_sectors)
            d_wr = max(0, cur.wr_sectors - prev.wr_sectors)
            rates[name] = IoRate(
                read_mbps=(d_rd * SECTOR_BYTES) / dt / 1_000_000,
                write_mbps=(d_wr * SECTOR_BYTES) / dt / 1_000_000,
            )
        self._last_counters = current
        self._last_ts = now_ts
        return rates
