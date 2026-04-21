---
title: Hostname rename
---

# Hostname rename

> **Stub.** Full walkthrough lands in v0.4.0.

This page will cover:

- Why hostname matters: mDNS publishes the box as `<hostname>.local` so you can reach the dashboard without remembering an IP
- The default hostname (`driveforge`) and what happens with multiple boxes on one LAN (avahi auto-suffixes the loser as `driveforge-2.local` non-deterministically)
- The Settings → Hostname panel (v0.2.8+): rename, hit Save, dashboard reachable under the new name within seconds
- What the rename does under the hood: `/etc/hostname`, `hostnamectl set-hostname`, `/etc/hosts` 127.0.1.1 patch, avahi-daemon restart
- Validation rules (RFC 1123 single-label, 1-63 chars, letters/digits/hyphens, no leading/trailing hyphen)
- Why the preseed doesn't prompt for hostname interactively at install time (intentional: keeps the unattended install flow intact; rename in Settings instead)
