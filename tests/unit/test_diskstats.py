from __future__ import annotations

import time
from unittest.mock import patch

from driveforge.core.diskstats import (
    DiskCounter,
    IoRateTracker,
    _is_partition,
    parse_diskstats,
)


# Captured from a real R720 running a mixed batch (sdd/sdi are WD 1 TB
# VelociRaptors mid-badblocks; sda is the USB boot drive; partitions are
# present under multiple whole-device entries to validate filtering).
_REAL_DISKSTATS_SAMPLE = """\
   8       0 sda 1234 56 78910 1234 5678 90 123456 7890 0 1234 9876
   8       1 sda1 10 0 20 5 0 0 0 0 0 5 5
   8       2 sda2 20 1 40 10 1 0 2 1 0 10 11
   8      16 sdb 100 0 500000 100 0 0 0 0 0 500 600
   8      32 sdc 200 0 800000 200 0 0 0 0 0 800 900
   8      48 sdd 50 0 250 50 9999 0 19500000 100000 0 50000 150000
   8      49 sdd1 1 0 2 1 0 0 0 0 0 1 1
   8      50 sdd2 2 0 4 2 0 0 0 0 0 2 2
   8     128 sdi 40 0 200 40 9888 0 19000000 95000 0 48000 145000
 259       0 nvme0n1 700 0 150000 700 100 0 20000 200 0 800 900
 259       1 nvme0n1p1 10 0 100 5 0 0 0 0 0 5 5
 252       0 dm-0 50 0 1000 50 10 0 500 20 0 50 70
  11       0 sr0 5 0 50 5 0 0 0 0 0 5 5
"""


def test_parse_diskstats_returns_whole_devices_only() -> None:
    counters = parse_diskstats(_REAL_DISKSTATS_SAMPLE)
    # Whole devices present
    assert "sda" in counters
    assert "sdb" in counters
    assert "sdd" in counters
    assert "sdi" in counters
    assert "nvme0n1" in counters
    assert "dm-0" in counters
    assert "sr0" in counters
    # Partitions filtered
    assert "sda1" not in counters
    assert "sdd1" not in counters
    assert "nvme0n1p1" not in counters


def test_parse_diskstats_extracts_correct_sector_columns() -> None:
    """Field 6 = rd_sectors, field 10 = wr_sectors (1-indexed, per the
    kernel's /proc/diskstats docs). Guard against off-by-one drift."""
    counters = parse_diskstats(_REAL_DISKSTATS_SAMPLE)
    # sdd wr_sectors = 19500000 (240 GB written). Badblocks in progress.
    assert counters["sdd"].wr_sectors == 19_500_000
    assert counters["sdd"].rd_sectors == 250
    # sdi: similar active-write profile
    assert counters["sdi"].wr_sectors == 19_000_000
    # sdb: read-only throughput sample
    assert counters["sdb"].rd_sectors == 500_000
    assert counters["sdb"].wr_sectors == 0


def test_parse_diskstats_tolerates_short_or_garbage_lines() -> None:
    """Production /proc/diskstats occasionally has trailing empty lines,
    pre-header whitespace, or malformed entries during hotplug — those
    must not take down the parser."""
    text = "\n   \nfoo bar\n   8   0\n" + _REAL_DISKSTATS_SAMPLE
    counters = parse_diskstats(text)
    # Still got every well-formed whole device.
    assert "sdd" in counters
    assert "sdi" in counters


def test_is_partition_classification() -> None:
    # Whole devices
    assert not _is_partition("sda")
    assert not _is_partition("sdz")
    assert not _is_partition("nvme0n1")
    assert not _is_partition("nvme12n3")
    assert not _is_partition("dm-0")
    assert not _is_partition("sr0")
    assert not _is_partition("zd0")
    # Partitions
    assert _is_partition("sda1")
    assert _is_partition("sdb12")
    assert _is_partition("nvme0n1p1")
    assert _is_partition("nvme0n1p12")
    assert _is_partition("mmcblk0p1")


def test_rate_tracker_first_poll_returns_empty_baseline() -> None:
    """Until we have two samples to diff, we can't compute a rate. First
    poll must return empty rather than reporting bogus zero or infinity."""
    tracker = IoRateTracker()
    with patch("driveforge.core.diskstats.read_diskstats") as mock_read:
        mock_read.return_value = {"sda": DiskCounter("sda", 100, 200)}
        assert tracker.poll() == {}


def test_rate_tracker_computes_mbps_from_delta() -> None:
    """sectors × 512 bytes / elapsed seconds / 1e6 = MB/s (decimal)."""
    tracker = IoRateTracker()
    with patch("driveforge.core.diskstats.read_diskstats") as mock_read, \
         patch("driveforge.core.diskstats.time.monotonic") as mock_clock:
        # Baseline sample at t=0
        mock_clock.return_value = 0.0
        mock_read.return_value = {"sda": DiskCounter("sda", 0, 0)}
        tracker.poll()
        # Second sample at t=5.0: 500_000 sectors written = 256 MB in 5 s = 51.2 MB/s
        mock_clock.return_value = 5.0
        mock_read.return_value = {"sda": DiskCounter("sda", 0, 500_000)}
        rates = tracker.poll()
    assert "sda" in rates
    assert rates["sda"].read_mbps == 0.0
    assert abs(rates["sda"].write_mbps - 51.2) < 0.01


def test_rate_tracker_handles_counter_rollover_as_zero() -> None:
    """In the rare case a counter appears to go backward (32-bit wrap,
    kernel quirk, disk re-enumeration after hotplug), clamp to zero
    rather than emitting a wildly negative rate."""
    tracker = IoRateTracker()
    with patch("driveforge.core.diskstats.read_diskstats") as mock_read, \
         patch("driveforge.core.diskstats.time.monotonic") as mock_clock:
        mock_clock.return_value = 0.0
        mock_read.return_value = {"sda": DiskCounter("sda", 1_000_000, 2_000_000)}
        tracker.poll()
        mock_clock.return_value = 3.0
        # Counters "went backward" — treat as zero activity for this tick.
        mock_read.return_value = {"sda": DiskCounter("sda", 500_000, 1_000_000)}
        rates = tracker.poll()
    assert rates["sda"].read_mbps == 0.0
    assert rates["sda"].write_mbps == 0.0


def test_rate_tracker_skips_zero_dt() -> None:
    """If the monotonic clock didn't advance (shouldn't happen, but guard
    against divide-by-zero on exotic platforms), return empty."""
    tracker = IoRateTracker()
    with patch("driveforge.core.diskstats.read_diskstats") as mock_read, \
         patch("driveforge.core.diskstats.time.monotonic") as mock_clock:
        mock_clock.return_value = 7.0
        mock_read.return_value = {"sda": DiskCounter("sda", 0, 0)}
        tracker.poll()
        mock_clock.return_value = 7.0  # same clock — dt == 0
        mock_read.return_value = {"sda": DiskCounter("sda", 100, 100)}
        assert tracker.poll() == {}
