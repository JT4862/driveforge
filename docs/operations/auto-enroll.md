---
title: Auto-enroll
---

# Auto-enroll

> **Stub.** Detailed flowcharts + decision tables land in v0.4.0.

This page will cover:

- The three modes — **Off**, **Quick**, **Full** — and what each runs
- How to toggle from the dashboard header pill
- When auto-enroll fires: hotplug ADD events, never on already-present drives at daemon start
- The "graded drives are sticky indefinitely" rule (v0.2.9+) — re-inserting a previously-tested drive does NOT auto-retest
- The aborted-drive case: aborted runs (grade=None) DO trigger auto-enroll on re-insert (the abort was a cancel, not a verdict)
- The recovery interaction: pulled-mid-erase drives go through recovery first, then a fresh auto-enroll
- Why auto-enroll is hotplug-only and what to do for shelf drives that should auto-run when plugged in
- Auto-enroll logs and how to debug "why didn't this drive auto-run?"
