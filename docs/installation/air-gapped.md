---
title: Air-gapped install
---

# Air-gapped install

> **Stub.** Detailed walkthrough lands in v0.4.0.

For environments where the target host has no internet access — common
for SCIF / classified / industrial-control deployments.

This page will cover:

- Building the offline bundle on an internet-connected build host (`scripts/build-offline-bundle.sh`)
- Bundle contents: apt debs (smartmontools, hdparm, sg3-utils, etc.), Python wheels, the DriveForge source tarball
- Transferring the bundle to the target (USB, internal mirror, sneakernet)
- Pointing `install.sh` at the bundle via `DRIVEFORGE_OFFLINE_BUNDLE=/path/to/bundle`
- What changes vs the standard install (apt sources.list points at file:// repo, pip uses local wheels)
- Update flow without internet: rebuild the bundle on a connected box, transfer, re-run install.sh

For now, see [`INSTALL.md`](https://github.com/JT4862/driveforge/blob/main/INSTALL.md) Path C.
