"""Known-flaky drive family advisories (v0.6.3+).

Some drive models have well-documented firmware issues — most
commonly with ATA security commands under SAT passthrough, but also
with SMART reporting, firmware-locked sectors, and passthrough
timeouts. When DriveForge recognizes such a drive by its model
string, we surface a one-line advisory on the drive card so operators
know what to expect BEFORE the pipeline kicks off — "ST3000DM001 has
known SAT passthrough issues; if pipeline hangs, pull the drive."

The advisory is informational only. It does NOT change pipeline
behavior — the pipeline still attempts the standard path, and v0.6.3+
auto-fallback kicks in if the drive misbehaves. The advisory just
sets operator expectation.

Maintenance: this is a small hardcoded list. We intentionally do NOT
maintain a comprehensive drive-family database (see the v0.7-era
scope-out in the backlog — benchmark tables were rejected for the
same maintenance-burden reason). Add models only when we have
CONCRETE evidence of systematic misbehavior on JT's own hardware or
from well-documented third-party sources (Backblaze reports,
manufacturer recalls, kernel bug reports). Speculation doesn't
belong here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DriveAdvisory:
    """A single known-flaky-family entry."""

    # Substring match against drive.model. Case-insensitive. Use the
    # most specific prefix that still catches all affected units
    # (e.g. "ST3000DM001" catches "-1CH166", "-9YN166", etc.).
    model_prefix: str

    # One-line, operator-facing advisory. Shown inline on the drive
    # card before pipeline start. Keep under ~120 chars so it renders
    # cleanly without wrapping.
    advisory: str

    # Short source note (not shown in UI; for backlog maintenance).
    source: str


# Keep this list small and evidence-based. Every entry should have a
# defensible source — either JT's own hardware observations or a
# well-known third-party report.
_KNOWN_FLAKY_FAMILIES: tuple[DriveAdvisory, ...] = (
    DriveAdvisory(
        model_prefix="ST3000DM001",
        advisory=(
            "Known-flaky firmware family (Seagate 3TB 'consumer'). "
            "May refuse SAT passthrough secure-erase; DriveForge will "
            "auto-fall-back to hdparm. Backblaze reported ~50% 3-yr "
            "failure rate. If pipeline hangs, pull and set aside."
        ),
        source=(
            "Backblaze Q3 2015 failure report; JT R720 2026-04-21 "
            "cascade hang (4-of-4 Seagates including two ST3000DM001)"
        ),
    ),
    DriveAdvisory(
        model_prefix="ST500LM012",
        advisory=(
            "Archive-class SMR drive — very slow on write-heavy "
            "workloads including badblocks burn-in. Pipeline may "
            "take 10x+ longer than similar-capacity CMR drives."
        ),
        source=(
            "Widely reported in Seagate SMR class-action aftermath; "
            "not observed on JT's hardware but included defensively"
        ),
    ),
)


def advisory_for(model: str | None) -> str | None:
    """Return the operator-facing advisory for a known-flaky model,
    or None if the model isn't on the list.

    Match is case-insensitive substring against `model_prefix`. The
    first match wins — list order matters if prefixes overlap, but
    we keep them non-overlapping by convention.

    Callers: dashboard drive card renderer (pre-pipeline advisory),
    and the failure banner (context when grading F after known-flaky
    patterns fire).
    """
    if not model:
        return None
    m = model.upper()
    for entry in _KNOWN_FLAKY_FAMILIES:
        if entry.model_prefix.upper() in m:
            return entry.advisory
    return None


def is_known_flaky(model: str | None) -> bool:
    """True iff the model is on the known-flaky list. Convenience
    wrapper around `advisory_for()` for call sites that only care
    about the boolean (e.g. CSS class toggle on the drive card)."""
    return advisory_for(model) is not None
