---
title: Grading rules
---

# Grading rules

> **Stub.** Full table + per-rule explanation lands in v0.4.0.

This page will cover:

- The five inputs to a grade decision: pre-test SMART, post-test SMART, short-test result, long-test result, badblocks errors
- The grade tiers — **A** (pristine), **B** (minor wear, deployable), **C** (heavy wear, redundancy-required), **Fail** (do not deploy)
- Reallocated-sector thresholds (defaults: A ≤ 3, B ≤ 8, C ≤ 40)
- The "no degradation during test" rule — any reallocation that occurred between pre and post → demotion
- Pending-sector and offline-uncorrectable counts (default: any > 0 → Fail)
- Thermal excursion — drives that hit > 60°C during burn-in get demoted to C (configurable in Settings → Grading)
- How to tune thresholds for your fleet's risk tolerance via the Settings UI
- The grading rationale array — every rule that fired is recorded in the TestRun row + cert PDF
