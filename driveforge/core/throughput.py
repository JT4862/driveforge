"""Throughput collection + stats for v0.5.6 throughput grading.

During badblocks, the orchestrator's diskstats sampler writes per-drive
MB/s numbers to `state.active_io_rate` every ~3 s. This module wraps
a small correlation layer: incoming samples are tagged with the
badblocks pass label (e.g. "pass 3/8 \u00b7 write 0xFF") that was active
when the sample landed, and at the end of the badblocks phase the
collector is finalized into per-pass means + overall percentiles.

Design decisions worth defending later:

* **No absolute-speed thresholds.** Grading rules built on this data
  compare the drive against itself \u2014 within-pass variance and
  pass-to-pass degradation \u2014 not against a benchmark table of
  expected MB/s per drive class. Benchmark tables go stale the moment
  new SKUs ship; self-referential rules don't.
* **Pass 1 skipped for degradation comparisons.** Consumer SSDs serve
  the first ~50-100 GB of writes from an SLC-mode cache and then fall
  back to slower native speeds. If pass 1 captures the cached speed
  and pass 8 captures the sustained speed, the "drive degraded"
  verdict would be a false positive. Comparing pass 2 vs pass 8
  sidesteps this entirely.
* **Zero-throughput samples dropped.** A read_mbps=0 write_mbps=0
  sample means the drive was between passes or the kernel's diskstats
  hadn't ticked yet; including those in the mean would depress
  healthy drives' throughput numbers.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class ThroughputStats:
    """Finalized throughput summary for one badblocks phase.

    All values are write throughput in MB/s (the destructive-write
    pattern is what badblocks-wise we actually care about; read
    throughput during verify passes is included in per_pass_means
    but `mean_mbps` is computed across all samples regardless of
    read/write direction).

    `per_pass_means` is ordered by pass index. A run that completed
    5 of 8 passes before being aborted yields a length-5 list.
    """

    mean_mbps: float | None
    p5_mbps: float | None
    p95_mbps: float | None
    per_pass_means: list[float] = field(default_factory=list)
    sample_count: int = 0


class ThroughputCollector:
    """Accumulator for per-pass throughput samples during badblocks.

    Usage:

        collector = ThroughputCollector()
        collector.note_pass("pass 1/8 \u00b7 write 0xAA")
        collector.note_sample(180.0)
        collector.note_sample(178.3)
        collector.note_pass("pass 2/8 \u00b7 write 0x55")
        collector.note_sample(175.1)
        ...
        stats = collector.finalize()

    Samples received before the first `note_pass` call are dropped
    silently (the sampling task wakes up before badblocks gets to
    emit its first progress line; those samples aren't meaningfully
    associated with any pass).
    """

    def __init__(self) -> None:
        self._current_pass: str | None = None
        # Ordered list of pass labels in the order they first appeared.
        self._pass_order: list[str] = []
        # Samples per pass. defaultdict so we don't have to initialize
        # on note_pass; lazy-initialized on first note_sample of a pass.
        self._samples_by_pass: dict[str, list[float]] = defaultdict(list)

    def note_pass(self, pass_label: str) -> None:
        """Mark the start of a new pass. Subsequent samples are
        attributed to this label until the next `note_pass` call."""
        if pass_label == self._current_pass:
            return
        self._current_pass = pass_label
        if pass_label not in self._pass_order:
            self._pass_order.append(pass_label)

    def note_sample(self, mbps: float) -> None:
        """Record one diskstats sample. Silently dropped if no pass
        is active yet (samples that arrived before badblocks emitted
        its first progress line) or if the sample is effectively zero
        (drive idle / between passes / kernel counters not yet
        advanced)."""
        if self._current_pass is None:
            return
        if mbps <= 0.1:
            return
        self._samples_by_pass[self._current_pass].append(mbps)

    def finalize(self) -> ThroughputStats:
        """Compute per-pass means + overall percentiles.

        Returns an all-None stats object when no samples were collected
        (e.g. diskstats unavailable for this device, or badblocks
        errored before any sample landed). Callers should persist the
        None result as-is \u2014 downstream grading + UI treat it as "no
        data" rather than as zero throughput.
        """
        all_samples: list[float] = []
        per_pass_means: list[float] = []
        for label in self._pass_order:
            samples = self._samples_by_pass.get(label, [])
            if not samples:
                continue
            per_pass_means.append(statistics.mean(samples))
            all_samples.extend(samples)

        if not all_samples:
            return ThroughputStats(
                mean_mbps=None,
                p5_mbps=None,
                p95_mbps=None,
                per_pass_means=[],
                sample_count=0,
            )

        sorted_samples = sorted(all_samples)
        return ThroughputStats(
            mean_mbps=statistics.mean(all_samples),
            p5_mbps=_percentile(sorted_samples, 5),
            p95_mbps=_percentile(sorted_samples, 95),
            per_pass_means=per_pass_means,
            sample_count=len(all_samples),
        )


def _percentile(sorted_samples: list[float], percentile: float) -> float:
    """Nearest-rank percentile. Simple + deterministic; for the
    sample sizes we care about (hundreds to thousands per run) the
    choice of interpolation method doesn't matter materially."""
    if not sorted_samples:
        raise ValueError("empty sample list")
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    rank = max(0, min(len(sorted_samples) - 1, int(len(sorted_samples) * percentile / 100)))
    return sorted_samples[rank]
