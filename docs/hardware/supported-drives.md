---
title: Supported drives
---

# Supported drives

> **Stub.** Detailed matrix + per-vendor notes land in v0.4.0.

This page will cover:

- **SATA HDDs** — supported, secure-erase via SAT passthrough (v0.3.0+); typically full-overwrite, hours per TB
- **SATA SSDs** — supported; many vendors do crypto-erase under the hood (Intel, Samsung enterprise) — finishes in seconds
- **SAS HDDs** — supported, sg_format --format; 15-60 min for 1 TB, scales with capacity
- **SAS SSDs** — supported; vendor-dependent crypto-erase via sg_format
- **NVMe** — supported, `nvme format -s 1`; near-instant crypto-erase
- **Self-encrypting drives (SED / Opal)** — currently treated as plain drives; SED-aware mode is a future feature
- **Drives with a HPA / DCO set** — current behavior is "honor the host-protected area"; a "wipe HPA + DCO too" option is a backlog item
- **Drives that report SMART unhealthy at enrollment** — automatic Fail; do not put in pipeline
- USB-attached drives — refused for safety
