"""Quick-pass triage — Clean / Watch / Fail verdict.

Quick pass skips the 8-pass badblocks burn-in and long self-test, so it
can't honestly award an A/B/C/F grade (the destructive pass is where
drives actually prove themselves). Instead it returns a triage verdict
that sorts drives into three buckets:

- **Clean** — no pending sectors, no climb during the run. Ship it.
- **Watch** — pending sectors present but stable. Recommend full pipeline
  before trusting the drive; it's probably fine but unverified.
- **Fail**  — pending or reallocated counters climbed DURING the quick
  pass itself. Don't ship — the drive is actively deteriorating.
  `settings.daemon.quick_pass_fail_action` controls what happens next
  (badge-only / prompt / auto-promote-to-full).

The thresholds are deliberately simple. Historical reallocated count
doesn't affect the verdict — a drive with 50 reallocations but stable
pending=0 for a year is fine; those are healed scars. Only two signals
matter: "are there suspicious sectors right now?" and "did it get
worse under observation?"

For full-pipeline runs, triage is not computed — the grade (A/B/C/F)
carries the verdict. See `driveforge.core.grading`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Triage(str, Enum):
    CLEAN = "clean"
    WATCH = "watch"
    FAIL = "fail"


class TriageResult(BaseModel):
    """Output of quick-pass triage.

    `verdict` is the headline bucket. `summary` is a short
    operator-facing string suitable for a dashboard tooltip or label.
    `pending_climbed` / `reallocated_climbed` surface the evidence so
    callers can render detailed explanations without re-deriving from
    the raw counters.
    """

    verdict: Triage
    summary: str
    pending_climbed: bool
    reallocated_climbed: bool


def triage_quick_pass(
    *,
    pre_pending: int | None,
    post_pending: int | None,
    pre_reallocated: int | None,
    post_reallocated: int | None,
) -> TriageResult:
    """Compute triage verdict from before/after SMART counters.

    Any of the four inputs may be None — on some drives / transports
    those attributes aren't exposed. The triage rules treat None as
    "unknown, can't prove a climb" and default toward Clean unless
    positive evidence of trouble appears.

    Decision logic (checked in order):

    1. If pending or reallocated *increased* during the run → Fail.
       The drive deteriorated under observation, which is the
       strongest possible signal that it's actively unhealthy.
    2. Otherwise, if post_pending > 0 → Watch.
       Pending sectors exist but are stable. Probably fine but not
       confirmed. Recommend full pipeline.
    3. Otherwise → Clean.
    """
    pending_climbed = _strictly_greater(post_pending, pre_pending)
    reallocated_climbed = _strictly_greater(post_reallocated, pre_reallocated)

    if pending_climbed or reallocated_climbed:
        parts = []
        if pending_climbed and pre_pending is not None and post_pending is not None:
            parts.append(f"pending {pre_pending} \u2192 {post_pending}")
        if reallocated_climbed and pre_reallocated is not None and post_reallocated is not None:
            parts.append(f"reallocated {pre_reallocated} \u2192 {post_reallocated}")
        climb_detail = "; ".join(parts) if parts else "counters climbed"
        return TriageResult(
            verdict=Triage.FAIL,
            summary=f"drive deteriorated during quick pass ({climb_detail})",
            pending_climbed=pending_climbed,
            reallocated_climbed=reallocated_climbed,
        )

    if post_pending is not None and post_pending > 0:
        return TriageResult(
            verdict=Triage.WATCH,
            summary=f"{post_pending} pending sector{'s' if post_pending != 1 else ''} detected \u2014 recommend full pipeline",
            pending_climbed=False,
            reallocated_climbed=False,
        )

    return TriageResult(
        verdict=Triage.CLEAN,
        summary="no pending sectors, no counter movement",
        pending_climbed=False,
        reallocated_climbed=False,
    )


def _strictly_greater(post: int | None, pre: int | None) -> bool:
    """Is `post` strictly greater than `pre`?

    Returns False when either side is None — a missing pre-snapshot
    (legacy run, smartctl failure) can't prove a climb. Conservative
    on purpose; better to miss a Fail verdict than to fail a drive
    based on ambiguous data.
    """
    if post is None or pre is None:
        return False
    return post > pre
