---
title: DriveForge documentation
---

# DriveForge

DriveForge turns server-class hardware (Dell R720, NX-3200, similar)
into an in-house drive refurbishment pipeline. Drop a drive in a bay,
the daemon SMART-checks it, secure-erases it, runs an 8-pass
badblocks burn-in (or skips it for a "quick" verdict), runs a SMART
long self-test, grades the result A/B/C/Fail, and prints a cert
label. Commercial-refurbisher workflow at homelab scale.

This documentation is **scaffolded as of v0.3.1** — the page tree is
in place, but most pages are short stubs that say "this will cover
…". Detailed content lands in **v0.4.0** (the documentation
release). Until then, [`BUILD.md`](https://github.com/JT4862/driveforge/blob/main/BUILD.md)
is the canonical project plan and architectural reference.

## Where to start

### Operators (running DriveForge on real hardware)

- [Installation](installation/) — flashing the ISO, booting, walking through the setup wizard
- [Operations](operations/) — daily use: dashboard tour, auto-enroll, identify LED, hostname rename, in-app updates
- [Hardware compatibility](hardware/) — supported HBAs, supported drives, known issues per hardware combo

### Reference

- [Reference](reference/) — grading rules, pull-recovery state machine, REST API

### Developers

- [`BUILD.md`](https://github.com/JT4862/driveforge/blob/main/BUILD.md) — architectural plan
- [`CONTRIBUTING.md`](https://github.com/JT4862/driveforge/blob/main/CONTRIBUTING.md) — development workflow
- [GitHub repo](https://github.com/JT4862/driveforge)
- [Releases](https://github.com/JT4862/driveforge/releases)

## Status

DriveForge is **pre-alpha**. Latest release is on the
[Releases page](https://github.com/JT4862/driveforge/releases).
Features are stable but the project hasn't yet had its v1.0
real-world burn-in pass.
