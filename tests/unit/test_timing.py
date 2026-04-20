from __future__ import annotations

from driveforge.core.timing import capacity_timeout


def test_capacity_timeout_unknown_returns_fallback() -> None:
    assert capacity_timeout(None) == 6 * 3600
    assert capacity_timeout(0) == 6 * 3600


def test_capacity_timeout_scales_linearly_with_capacity() -> None:
    # 100 MB/s pessimistic → 10 s/GB, headroom 2× → 20 s/GB for 1 pass.
    one_tb = capacity_timeout(1_000_000_000_000)
    four_tb = capacity_timeout(4_000_000_000_000)
    eight_tb = capacity_timeout(8_000_000_000_000)
    sixteen_tb = capacity_timeout(16_000_000_000_000)
    # Exact arithmetic check: 1 TB * 10 s/GB * 1 pass * 2x = 20,000 s (5.5 h)
    assert one_tb == 20_000
    assert four_tb == 80_000
    assert eight_tb == 160_000
    assert sixteen_tb == 320_000


def test_capacity_timeout_multi_pass_badblocks() -> None:
    # 8-pass badblocks on 4 TB: 4000 * 10 * 8 * 2 = 640,000 s (~178 h / 7.4 d)
    t = capacity_timeout(4_000_000_000_000, passes=8)
    assert t == 640_000
    # 8 TB: ~15 days. That's expected — a full 8-pass burn-in on a modern
    # capacity drive legitimately takes that long at spinning-rust speeds.
    t = capacity_timeout(8_000_000_000_000, passes=8)
    assert t == 1_280_000


def test_capacity_timeout_tiny_drive_hits_floor() -> None:
    # 128 GB SSD: 128 * 10 * 1 * 2 = 2_560 s — below the 1-hour floor.
    # Must return the floor so hdparm setup overhead doesn't get squeezed.
    t = capacity_timeout(128_000_000_000)
    assert t == 3600


def test_capacity_timeout_custom_headroom() -> None:
    # User can tighten or relax the multiplier for specialized callers.
    assert capacity_timeout(1_000_000_000_000, headroom=1.5) == 15_000
    assert capacity_timeout(1_000_000_000_000, headroom=3.0) == 30_000


def test_capacity_timeout_does_not_cap_large_drives() -> None:
    """Regression: the old 16 h cap on SATA erase and 72 h cap on badblocks
    silently killed jobs on 6+ TB drives. There must be NO implicit upper
    bound — if a drive needs 5 days, it gets 5 days. The operator aborts."""
    huge = capacity_timeout(100_000_000_000_000, passes=8)  # 100 TB hypothetical
    assert huge > 16 * 3600
    assert huge > 72 * 3600
    assert huge > 24 * 3600 * 7  # more than a week, no silent ceiling
