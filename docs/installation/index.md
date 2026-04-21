---
title: Installation
---

# Installation

DriveForge ships as an installer ISO (Debian netinst + preseed +
late_command). For most operators that's the whole flow: flash USB,
boot, pick the OS disk, walk away. The dashboard is reachable at
`http://driveforge.local:8080` once the install finishes.

## Paths

- **[ISO install](iso-install.md)** — the recommended path. Flash the
  release ISO to a USB stick, boot the target, walk through the setup
  wizard. (Stub for v0.4.0.)
- **[Manual install](manual-install.md)** — clone the repo, run
  `scripts/install.sh` on an existing Debian 12 host. For homelab
  operators who already have a Debian box and don't want to reinstall
  the OS. (Stub for v0.4.0.)
- **[Air-gapped install](air-gapped.md)** — for environments with no
  internet access on the target. Uses a pre-built offline bundle of
  apt debs + Python wheels. (Stub for v0.4.0.)

For now, see [`INSTALL.md`](https://github.com/JT4862/driveforge/blob/main/INSTALL.md) in the repo root.
