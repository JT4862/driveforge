---
title: Installation
---

# Installation

DriveForge ships as an installer ISO (Debian netinst + preseed +
late_command). For most operators that's the whole flow: flash USB,
boot, pick the OS disk, walk away. The ISO includes two boot-menu
entries — default **DriveForge** for the first box on your network
(pick Standalone or Operator via the setup wizard), and **DriveForge
Agent** for the 2nd, 3rd, Nth box joining an existing fleet (no
wizard, auto-joins via mDNS).

## Paths

- **[ISO install](iso-install.md)** — the recommended path. Flash the
  release ISO to a USB stick, boot the target, walk through the setup
  wizard (or pick the Agent entry for headless fleet members).
- **[Manual install](manual-install.md)** — clone the repo, run
  `scripts/install.sh` on an existing Debian 12 host. For homelab
  operators who already have a Debian box and don't want to reinstall
  the OS.
- **[Air-gapped install](air-gapped.md)** — for environments with no
  internet access on the target. Uses a pre-built offline bundle of
  apt debs + Python wheels.

For multi-box deployments see [Fleet mode](../operations/fleet.md) —
walks through installing the operator + adding agent boxes end to end.
