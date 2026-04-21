---
title: Grading rules
---

# Grading rules

DriveForge grades each drive **A**, **B**, **C**, or **Fail**.
Grading is **transparent** — every grade includes a list of the
specific rule evaluations that produced it. The rationale shows up
on the drive detail page and in the cert PDF.

## The grade tiers

| Grade | Meaning | Suggested use |
|-------|---------|---------------|
| **A** | Pristine: ≤ 3 reallocated sectors, no growth during burn-in, all tests passed | Primary Ceph OSD, TrueNAS main pool — no reservations |
| **B** | Minor wear: ≤ 8 reallocated sectors, no growth, all tests passed | Secondary OSD, scratch pool, backup target |
| **C** | Heavy wear (but stable): ≤ 40 reallocated sectors, no growth, all tests passed | Cold storage, test environment, heavy-redundancy array |
| **Fail** | Any failure: SMART status fails, growing reallocations during test, > 40 starting reallocated, pending sectors, offline uncorrectable, badblocks errors, self-test failed | Scrap / e-waste — do not deploy |

The thresholds (3 / 8 / 40) are tunable in **Settings → Grading**.

## The five inputs to a grade decision

Grading takes a snapshot of:

1. **Pre-test SMART** — captured before any erase or burn-in
2. **Post-test SMART** — captured after all destructive testing
3. **SMART short self-test result** — pass / fail / unsupported
4. **SMART long self-test result** — pass / fail / unsupported (skipped in Quick mode)
5. **badblocks error counts** — read / write / compare error counts after 8-pass destructive sweep (skipped in Quick mode)

Plus an optional sixth: **max temperature observed during the run**,
used for the thermal-excursion rule.

## The rules, in order

`grade_drive()` in `driveforge/core/grading.py` runs these rules in
sequence. Each rule produces a `Rule` object with `name`, `passed`,
`detail`, and optionally a `forces_grade` (which clamps the grade at
that tier or below).

### Test-outcome rules (any failure = Fail)

- **`smart_short_test_passed`** — short self-test outcome. `False` →
  Fail. `None` (drive doesn't support short tests) → neutral, the
  rationale notes it.
- **`smart_long_test_passed`** — same semantics for the long
  self-test. Quick-mode runs skip the long test, so this rule is
  neutral in Quick mode.
- **`badblocks_clean`** — total of read + write + compare errors
  must be 0. Any non-zero → Fail. Quick-mode skips badblocks; rule
  is reported as clean in that case.

### SMART-counter floor rules (configurable, default Fail on >0)

- **`no_pending_sectors`** — `current_pending_sector` count must be
  0. Configurable via `fail_on_pending_sectors` (default `true`).
  Pending sectors are sectors the drive's firmware has flagged for
  remap on next write — strong predictor of imminent failure per
  Backblaze multi-year data.
- **`no_offline_uncorrectable`** — `offline_uncorrectable` count
  must be 0. Configurable via `fail_on_offline_uncorrectable`
  (default `true`). These are sectors the drive could not correct
  even with retries — also a strong failure predictor.

### Degradation rules (always Fail on growth)

For each of `reallocated_sectors`, `current_pending_sector`,
`offline_uncorrectable`:

- **`no_degradation_<attr>`** — post-test value must not exceed
  pre-test value. Any growth → Fail.

The intuition: a drive that actively deteriorated on the bench
(reallocated more sectors during your test than it had before) is
unstable. Even if the absolute counts are within Grade A thresholds,
the trend matters more than the snapshot.

### Tier-determining rule (Grade A vs B vs C)

- **`grade_<X>_reallocated`** — based on `post.reallocated_sectors`
  vs the configured thresholds:

| reallocated_sectors | Tier cap |
|---------------------|----------|
| ≤ `grade_a_reallocated_max` (default 3) | A |
| ≤ `grade_b_reallocated_max` (default 8) | B |
| ≤ `grade_c_reallocated_max` (default 40) | C |
| > `grade_c_reallocated_max` | Fail |

The thresholds are deliberately permissive at the A tier — Grade A
used to require strictly 0 reallocated sectors, but every commercial
drive ships with a spare-sector pool and a handful of stable
reallocations has no correlation with imminent failure (per Backblaze
fleet data). 3 is a good "pristine with minor wear" ceiling.

### Thermal excursion (optional, demotes only)

- **`thermal_excursion`** — if max observed drive temperature during
  the run exceeded `thermal_excursion_c` (default 60°C), the drive
  is **demoted to C** (regardless of reallocated_sectors tier).
  Doesn't fail outright — just clamps the verdict.

Set `thermal_excursion_c` to `null` (blank in the Settings UI) to
disable.

### Power-on hours sanity (drift tolerance)

- **`power_on_hours_drift_tolerance_h`** (default 1) — if `post.power_on_hours - pre.power_on_hours` differs from the actual run wall-clock by more than this, log a warning. Doesn't affect grade. Drives whose POH counter doesn't tick correctly during a multi-hour test are flagged for operator awareness — usually a firmware bug, occasionally a sign of a drive that's been wildly mis-clocked.

## How the final grade is computed

After all rules run:

1. If any rule has `forces_grade=Grade.FAIL`, the drive is **Fail**.
2. Otherwise, take the highest tier compatible with the
   `tier_cap` from the reallocated-sectors rule.
3. If thermal excursion fired, clamp to **C**.
4. Final grade is the result.

The `rules` list and a human-readable `rationale` summary both go
into the TestRun row's `rules` column (JSON) and render on the
drive detail page + cert PDF.

## Configuring thresholds

**Settings → Grading thresholds.** Form fields:

- **Grade A — max reallocated sectors** (default 3)
- **Grade B — max reallocated sectors** (default 8)
- **Grade C — max reallocated sectors** (default 40)
- **Fail on any current-pending-sector count > 0** (default checked)
- **Fail on any offline-uncorrectable count > 0** (default checked)
- **Thermal excursion — demote to C above this °C** (default 60;
  blank to disable)

Saved to `/etc/driveforge/driveforge.yaml` under the `grading:`
section. Daemon reads live; no restart needed.

## Tuning for your fleet

Conservative (stricter than defaults — for drives going into mission-critical
storage):

| Setting | Conservative |
|---------|--------------|
| Grade A max reallocated | 0 |
| Grade B max reallocated | 3 |
| Grade C max reallocated | 10 |
| Thermal excursion | 55 |

Permissive (looser than defaults — for cold-storage / archival use):

| Setting | Permissive |
|---------|------------|
| Grade A max reallocated | 10 |
| Grade B max reallocated | 30 |
| Grade C max reallocated | 100 |
| Fail on pending sectors | unchecked (manual review) |
| Thermal excursion | (blank, disabled) |

The defaults aim at the middle: drives going into a typical homelab
TrueNAS / Ceph cluster.

## Why grading is transparent

Refurbishment is fundamentally a trust exercise — someone is
shipping a drive to a customer based on a verdict the tool produced.
A black-box "this drive is Grade A" verdict makes that hard to
justify. DriveForge's grading rationale is meant to be auditable:

- Every rule that fired is recorded in the DB
- The cert PDF reproduces the rule list
- The drive detail page surfaces it for live drives

If you disagree with a verdict, you can see exactly which rule
caused it and either re-test, override manually, or argue with the
threshold defaults.
