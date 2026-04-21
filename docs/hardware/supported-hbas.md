---
title: Supported HBAs
---

# Supported HBAs

> **Stub.** Detailed compatibility matrix lands in v0.4.0.

This page will cover:

- **LSI 9207-8i** (R720 crossflashed PERC H710) — fully supported, all transports
- **LSI SAS2308** (NX-3200) — fully supported as of v0.3.0 (SAT passthrough); pre-v0.3.0 had SATA-erase issues from `CONFIG_IDE_TASK_IOCTL`
- **Generic SAS HBAs** — anything SAT-3 conformant (mandatory since 2008) should work; SAT passthrough is the universal SATA path
- **Direct motherboard SATA** (no SAS HBA) — works via Linux libata's SAT shim
- **Hardware RAID controllers** — NOT supported; flash to IT mode or use a real HBA
- **USB-SATA bridges** — explicitly refused for safety (likely external boot drives, not test targets)
- IT-mode firmware notes: required for the LSI 9200/9207 series; instructions for crossflashing
