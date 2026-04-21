---
title: Pull and recover
---

# Pull and recover

> **Stub.** Full state-machine diagram lands in v0.4.0.

This page will cover:

- What happens when you yank a drive mid-pipeline (v0.2.2 → v0.2.5 evolution)
- Detection: hotplug REMOVE event flags `state.interrupted_serials` AND `_looks_like_pull` device-existence/serial-rediscovery check (v0.2.9+)
- The TestRun goes "open" with `interrupted_at_phase` set, `completed_at NULL`
- Re-insert triggers `recover_drive()` → repair drive state → fresh pipeline
- Per-transport repair:
  - SAS: complete the interrupted `sg_format --format` (15-60+ min)
  - SATA: SAT-passthrough security unlock + disable (v0.3.0+) — was hdparm pre-v0.3.0
  - NVMe: no-op (crypto-erase is atomic)
- The persistent amber-glow recovery-mode indicator on the card (v0.2.5+) for the entire recovery duration
- Edge cases:
  - Daemon crash mid-pipeline → startup sweep flags dangling runs as interrupted
  - Drive was present at daemon-start but never pulled → present-at-boot recovery dispatch
  - Bus glitch / kernel re-enumeration without a real pull (v0.2.9 _looks_like_pull fix)
