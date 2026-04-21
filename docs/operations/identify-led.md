---
title: Identify LED
---

# Identify LED

> **Stub.** Detailed walkthrough lands in v0.4.0.

This page will cover:

- The Ident button on every Installed-row drive card (v0.2.9+)
- What the button does on different chassis:
  - SES-capable backplanes (Dell MD1200-class, etc.) → blue locate LED via `ledctl locate=`
  - SAS expander-only backplanes (NX-3200) → activity LED rapid strobe via I/O bursts
  - Direct-attach LFF (R720 LFF) → activity LED rapid strobe via I/O bursts
- Toggle behavior: click again → Stop, restoring the prior pass/fail LED pattern
- Auto-stop after 5 minutes (matches iDRAC/iLO defaults; safety against forgotten ident)
- Why the button is disabled / hidden on Active drives (the pipeline is already lighting the activity LED)
