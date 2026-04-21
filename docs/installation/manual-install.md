---
title: Manual install
---

# Manual install (existing Debian 12 host)

> **Stub.** Detailed walkthrough lands in v0.4.0.

For homelab operators who already have a Debian 12 box and want to
install DriveForge on top of it without reinstalling the OS.

This page will cover:

- Prerequisites: Debian 12, root access, internet egress for apt + GitHub
- The single-line install: `curl -sSL .../install.sh | sudo bash`
- What `install.sh` does: package installs, user creation, systemd unit setup, daemon bootstrap
- Post-install: dashboard URL, where state and config live (`/etc/driveforge`, `/var/lib/driveforge`, `/var/log/driveforge`)
- Re-running install.sh to pick up code changes (the manual update path before v0.3.1's in-app update)

For now, see [`INSTALL.md`](https://github.com/JT4862/driveforge/blob/main/INSTALL.md) Path B.
