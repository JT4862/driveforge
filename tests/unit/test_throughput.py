"""Tests for driveforge.core.throughput \u2014 the ThroughputCollector +
finalized-stats path used by v0.5.6 throughput grading.

All grading-relevant behavior is tested here: per-pass mean accuracy,
percentile computation, empty-collector handling, zero-throughput
filtering, and sample-before-pass-start drop behavior. The grading
rules themselves live in test_grading.py.
"""

from __future__ import annotations

import pytest

from driveforge.core.throughput import ThroughputCollector, ThroughputStats


def test_empty_collector_returns_none_stats() -> None:
    """A collector that never saw a pass or a sample must finalize
    into all-None stats so downstream persistence can tell "we didn't
    measure anything" from "we measured 0 MB/s.\""""
    stats = ThroughputCollector().finalize()
    assert stats.mean_mbps is None
    assert stats.p5_mbps is None
    assert stats.p95_mbps is None
    assert stats.per_pass_means == []
    assert stats.sample_count == 0


def test_samples_before_first_pass_are_dropped() -> None:
    """The diskstats sampler wakes up every 3 s regardless of badblocks
    progress, so samples often land before badblocks has emitted its
    first progress line. Those can't be attributed to any pass, so
    they must be silently dropped rather than lumped into pass 1."""
    c = ThroughputCollector()
    c.note_sample(150.0)  # no pass yet
    c.note_sample(160.0)
    stats = c.finalize()
    assert stats.sample_count == 0
    assert stats.per_pass_means == []


def test_zero_samples_dropped() -> None:
    """A sample of 0.0 MB/s means the drive was idle between passes
    or diskstats hadn't ticked \u2014 not a real measurement. Including
    zero samples in the mean would depress healthy drives' numbers."""
    c = ThroughputCollector()
    c.note_pass("pass 1/8 \u00b7 write 0xAA")
    c.note_sample(0.0)
    c.note_sample(0.05)  # below the 0.1 floor, still dropped
    c.note_sample(180.0)
    c.note_sample(0.0)
    stats = c.finalize()
    assert stats.sample_count == 1
    assert stats.mean_mbps == 180.0


def test_single_pass_basic_stats() -> None:
    c = ThroughputCollector()
    c.note_pass("pass 1/8 \u00b7 write 0xAA")
    for v in (170, 175, 180, 185, 190):
        c.note_sample(v)
    stats = c.finalize()
    assert stats.sample_count == 5
    assert stats.mean_mbps == pytest.approx(180.0)
    assert len(stats.per_pass_means) == 1
    assert stats.per_pass_means[0] == pytest.approx(180.0)


def test_multi_pass_preserves_pass_order() -> None:
    """per_pass_means must be ordered by first-seen pass label, not
    alphabetical or insertion-into-dict order. Downstream grading
    rules (pass-to-pass degradation) rely on this ordering."""
    c = ThroughputCollector()
    c.note_pass("pass 1/8 \u00b7 write 0xAA")
    c.note_sample(200)
    c.note_sample(200)
    c.note_pass("pass 2/8 \u00b7 write 0x55")
    c.note_sample(180)
    c.note_sample(180)
    c.note_pass("pass 3/8 \u00b7 write 0xFF")
    c.note_sample(160)
    c.note_sample(160)

    stats = c.finalize()
    assert stats.per_pass_means == [200.0, 180.0, 160.0]


def test_duplicate_pass_label_does_not_restart_tracking() -> None:
    """Progress callback may fire multiple times with the same
    pass_label as badblocks emits progress within a pass. Repeated
    note_pass with an already-active label must be a no-op, not
    start a new pass entry."""
    c = ThroughputCollector()
    c.note_pass("pass 1/8 \u00b7 write 0xAA")
    c.note_pass("pass 1/8 \u00b7 write 0xAA")  # duplicate
    c.note_pass("pass 1/8 \u00b7 write 0xAA")  # again
    c.note_sample(180)
    c.note_pass("pass 2/8 \u00b7 write 0x55")
    c.note_sample(170)

    stats = c.finalize()
    assert len(stats.per_pass_means) == 2


def test_percentiles_roughly_correct() -> None:
    """p5 and p95 are nearest-rank (not interpolated), so the exact
    value depends on sample count. For 100 evenly-spaced samples,
    p5 should land near the low end and p95 near the high end."""
    c = ThroughputCollector()
    c.note_pass("pass 1/8 \u00b7 write 0xAA")
    for v in range(100, 200):  # 100 samples, mean = 149.5
        c.note_sample(float(v))
    stats = c.finalize()
    assert stats.mean_mbps == pytest.approx(149.5)
    assert 100 <= stats.p5_mbps <= 110, f"p5 {stats.p5_mbps} should be near low end"
    assert 190 <= stats.p95_mbps <= 200, f"p95 {stats.p95_mbps} should be near high end"


def test_pass_with_no_samples_excluded_from_means() -> None:
    """If a pass label is announced but no samples land before the
    next pass starts (drive was idle, very short pass, whatever),
    that pass must not appear in per_pass_means as an entry."""
    c = ThroughputCollector()
    c.note_pass("pass 1/8 \u00b7 write 0xAA")
    c.note_sample(180)
    c.note_pass("pass 2/8 \u00b7 write 0x55")
    # no samples for pass 2
    c.note_pass("pass 3/8 \u00b7 write 0xFF")
    c.note_sample(170)

    stats = c.finalize()
    assert stats.per_pass_means == [180.0, 170.0]


def test_slc_cache_simulation_preserves_full_history() -> None:
    """Simulate a consumer SSD: pass 1 is fast (SLC cache), passes
    2-8 drop to sustained speed. The collector must faithfully record
    all 8 passes; the *grading* rule (skip pass 1, compare pass 2
    vs pass 8) is what handles the SLC cache edge case, not the
    collector."""
    c = ThroughputCollector()
    # Pass 1 \u2014 SLC cached, fast
    c.note_pass("pass 1/8 \u00b7 write 0xAA")
    for _ in range(30):
        c.note_sample(500.0)
    # Passes 2-8 \u2014 sustained TLC, slow
    for i in range(2, 9):
        c.note_pass(f"pass {i}/8 \u00b7 write 0xXX")
        for _ in range(30):
            c.note_sample(120.0)

    stats = c.finalize()
    assert len(stats.per_pass_means) == 8
    assert stats.per_pass_means[0] == pytest.approx(500.0)
    for v in stats.per_pass_means[1:]:
        assert v == pytest.approx(120.0)
    # The grading rule (tested in test_grading.py) will skip pass 1
    # and conclude pass 2 vs pass 8 ratio = 120/120 = 1.0 \u2192 no
    # degradation. Collector's job is just to keep the history.
