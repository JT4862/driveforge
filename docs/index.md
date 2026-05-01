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

**Single-box or fleet.** Single-box is the default and covers most
homelab deployments. **Fleet mode (v0.10+)** lets one operator
DriveForge aggregate drives from additional agent DriveForge boxes
onto one dashboard — useful when you have spare servers that could
burn-in drives but don't need their own UI. See
[Fleet mode](operations/fleet.md) for the multi-node guide.

**Full operator documentation as of v1.0.** Every page in the tree
below has detailed content. The v0.11.x series brought fleet mode
to production-readiness — fleet-wide one-click updates with
verified delivery, batch creation across multiple hosts, agent
drives showing real test history on the operator dashboard, and
remediation panels that work for both standalone and fleet drives.
v1.0 was the formal stamp on top of v0.11.13 after the first
24-drive multi-host batch ran end-to-end in production. For the
architectural / design plan, see
[`BUILD.md`](https://github.com/JT4862/driveforge/blob/main/BUILD.md)
in the repo root — that's still the canonical place for "why is the
system designed this way?" decisions.

## Where to start

### Operators (running DriveForge on real hardware)

- [Installation](installation/) — flashing the ISO, booting, walking through the setup wizard
- [Operations](operations/) — daily use: dashboard tour, fleet setup, auto-enroll, identify LED, hostname rename, in-app updates
- [Hardware compatibility](hardware/) — supported HBAs, supported drives, known issues per hardware combo
- [Fleet mode](operations/fleet.md) — multi-node deployments: operator + agents, one dashboard

### Reference

- [Reference](reference/) — grading rules, pull-recovery state machine, REST API

### Developers

- [`BUILD.md`](https://github.com/JT4862/driveforge/blob/main/BUILD.md) — architectural plan
- [`CONTRIBUTING.md`](https://github.com/JT4862/driveforge/blob/main/CONTRIBUTING.md) — development workflow
- [GitHub repo](https://github.com/JT4862/driveforge)
- [Releases](https://github.com/JT4862/driveforge/releases)

## Status

DriveForge is **v1.0** as of 2026-04-24 — production-ready and
validated end-to-end on real hardware. Latest release is on the
[Releases page](https://github.com/JT4862/driveforge/releases).
v1.0 was tagged after a 3-node fleet (Dell R720 + Supermicro/Nutanix
NX-3200 + Seneca xVault) ran a 24-drive quick-mode batch under
operator control with the v0.11.13 codebase, validating: fleet
fan-out, batch_id propagation across hosts, virtual-media drive
filtering, and the in-app fleet update path with verified delivery.
