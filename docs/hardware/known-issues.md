---
title: Known issues
---

# Known issues

> **Stub.** Live-maintained issues list lands in v0.4.0.

This page will cover (initial list to expand):

- **`CONFIG_IDE_TASK_IOCTL` on SATA-on-SAS** (pre-v0.3.0) — fixed in v0.3.0 by SAT passthrough; if you see it, upgrade
- **Stuck `interrupted_at_phase` runs from bus glitches** (pre-v0.2.9) — fixed in v0.2.9 by serial-rediscovery in `_looks_like_pull`
- **Re-test churn from re-inserting graded drives** (pre-v0.2.9) — fixed in v0.2.9 by indefinite-graded-lock in auto-enroll
- **`driveforge.local` collision on multi-box LANs** (pre-v0.2.8) — fixed in v0.2.8 by Settings → Hostname
- **`driveforge-issue.service` exit=2 cosmetic noise** (pre-v0.2.9) — fixed in v0.2.9
- **Brother QL printer support** — code wired but no on-hand hardware test yet (waiting on hardware)
- **Amber fault LED via SES** — works in code; needs SES-capable backplane (R720 LFF + NX-3200 expander-only can't exercise)
- **Hung `hdparm -B254` D-state from earlier code paths** — observed once on the R720; unclear if reproducible
