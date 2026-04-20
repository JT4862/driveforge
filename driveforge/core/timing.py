"""Capacity-based phase timeouts.

Both secure-erase and full-disk badblocks scale linearly with capacity.
Early iterations hardcoded caps (16 h SATA erase, 12 h SAS erase, 72 h
badblocks, 24 h long self-test) that all broke silently at drive sizes
≥ 6 TB: a 4 TB drive timed out mid-erase on 2026-04-20, and anything
larger would have killed the rest of the pipeline too.

The right model is: figure out how long the phase would take on a
pessimistically-slow drive, add generous headroom, and let the phase
run as long as it needs. A 16 TB drive legitimately needs ~5 days to
finish an 8-pass badblocks run; the orchestrator should allow that,
and the operator can always abort if something's actually hung.

Pessimistic throughput of 100 MB/s covers 7200 RPM HDDs (typically
150-220 MB/s sustained), SMR drives (throttle to 40-80 MB/s during
large writes), and 2.5" 5400 RPM laptop drives (80-120 MB/s).
Everything else finishes well under the resulting budget.
"""

from __future__ import annotations

# 100 MB/s pessimistic sustained throughput → 10 seconds per GB of I/O.
SECONDS_PER_GB_PESSIMISTIC = 10


def capacity_timeout(
    capacity_bytes: int | None,
    *,
    passes: int = 1,
    headroom: float = 2.0,
    floor_seconds: int = 3600,
    fallback_seconds: int = 6 * 3600,
) -> int:
    """Return a capacity-scaled phase timeout in seconds.

    Args:
        capacity_bytes: Drive's raw capacity. If unknown, returns
            `fallback_seconds` — better than guessing zero and failing
            immediately.
        passes: How many times the full disk is written. 1 for a single-
            pass secure erase (hdparm, sg_format, nvme format), 8 for
            `badblocks -w` (4 patterns × write+verify). Long self-test is
            effectively 1 full read pass so use 1.
        headroom: Multiplier on the straight-line estimate. Default 2×
            covers real-world variance: thermal throttling, SMR shingling
            stalls on 4+ TB desktop drives, and firmware pauses on enterprise
            SAS. 1.5× is too tight (we saw 4 TB drives finish in 5h17m
            of a 6h cap).
        floor_seconds: Minimum returned timeout even for tiny drives —
            128 GB SSDs would otherwise get ~4 min, which is shorter than
            hdparm's setup overhead. 1 h floor is generous for small media.
        fallback_seconds: Returned when capacity_bytes is unknown/zero.
            6 h is enough for any drive up to ~1 TB at pessimistic rates.

    Examples:
        1 TB secure-erase: 1000 × 10 × 1 × 2 = 20,000 s (5.5 h)
        4 TB secure-erase: 4000 × 10 × 1 × 2 = 80,000 s (22 h)
        8 TB secure-erase: 8000 × 10 × 1 × 2 = 160,000 s (44 h)
        16 TB secure-erase: 16000 × 10 × 1 × 2 = 320,000 s (89 h, ~3.7 d)
        1 TB badblocks-8: 1000 × 10 × 8 × 2 = 160,000 s (44 h)
        8 TB badblocks-8: 8000 × 10 × 8 × 2 = 1,280,000 s (355 h, ~15 d)
    """
    if not capacity_bytes:
        return fallback_seconds
    gb = capacity_bytes / 1_000_000_000
    seconds = int(gb * SECONDS_PER_GB_PESSIMISTIC * passes * headroom)
    return max(floor_seconds, seconds)
