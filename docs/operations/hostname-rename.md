---
title: Hostname rename
---

# Hostname rename

*Available since v0.2.8.*

DriveForge boxes default to hostname `driveforge` and publish
themselves on the LAN as `driveforge.local` via mDNS (avahi). On a
LAN with one DriveForge box, that's perfect. On a LAN with two, it's
a problem.

## The problem this fixes

Both boxes try to claim `driveforge.local`. avahi breaks the tie by
auto-suffixing the loser as `driveforge-2.local` (then
`driveforge-3.local`, etc.). **Which box gets which name is
non-deterministic across reboots.** The URL you bookmarked for "the
storage rack in the loft" might tomorrow point at "the rack in the
basement."

The fix is simple: give each box a distinct hostname.

## How to rename

**Settings → Hostname** panel. Type a name, hit **Save hostname**,
done. The new name is live within a few seconds — no reboot required.

The validation rules show on the form:

- **1–63 characters**
- **Letters, digits, hyphens only**
- **Must not start or end with a hyphen**
- **Must not be all-numeric** (would look like an IP address to
  resolvers and confuse them)
- Reserved names rejected: `localhost`, `localdomain`, `ip6-localhost`,
  `ip6-loopback`

These follow RFC 1123 single-label hostnames. Whitespace is stripped,
case is lowercased, so `  Forge-Rack-B  ` becomes `forge-rack-b`.

## What happens under the hood

Four steps, each idempotent:

1. **Atomic write to `/etc/hostname`** via temp-file + rename. If the
   write fails, nothing else runs.
2. **`hostnamectl set-hostname <name>`** so systemd + the kernel
   pick up the change. This is the same command Debian uses
   internally for hostname changes.
3. **Patch `/etc/hosts`** — the `127.0.1.1` row gets rewritten to
   point at the new hostname (Debian convention; sudo emits warnings
   if this drifts from the system hostname). Other rows in
   `/etc/hosts` are preserved verbatim. If no `127.0.1.1` row exists,
   one is appended.
4. **Restart `avahi-daemon`** so mDNS immediately re-publishes under
   the new name. Without this, the old hostname stays advertised
   until the next service restart or reboot.

If step 4 fails (avahi-daemon not running, etc.), the rename is
still considered successful — the OS hostname changed correctly,
mDNS will catch up on the next avahi restart or reboot.

## After the rename

The dashboard URL becomes `http://<newname>.local:8080`. The old
URL stops resolving within ~30 seconds (mDNS clients cache the old
name briefly).

If you had the dashboard open under the old hostname when you
clicked Save, the page redirect after the form submit goes to
`/settings?saved=hostname` — which uses the request's `Host` header
(still the old name) to construct the redirect. Just navigate to
the new URL manually after saving; the bookmark you've been using
becomes stale.

## Why the preseed doesn't prompt at install time

The Debian installer runs at debconf priority `critical` (everything
unattended after the boot-disk pick). The `netcfg/get_hostname`
question has native priority `high`. Forcing it to ask would require
dropping global priority to `high`, which would also re-prompt for
a bunch of other questions we currently auto-answer (mirror
selection, root password, full-disk-encryption opt-in).

Settings-UI-only reaches the same end state without compromising
the "flash USB, walk away" flow. Multi-box operators rename on
first login.

## Multi-box workflow

For a homelab with several DriveForge installs:

1. Flash one USB stick with the latest ISO. Use it for every box.
2. Boot the first box, walk through setup, **immediately rename**
   to e.g. `forge-loft` via Settings → Hostname.
3. Boot the second box, walk through setup, rename to e.g.
   `forge-basement`.
4. Bookmark `http://forge-loft.local:8080` and `http://forge-basement.local:8080`.

The hostnames are stable across reboots — `/etc/hostname` is
on-disk state, not a runtime decision.

## What if mDNS doesn't work on my network?

Some networks block multicast (corporate Wi-Fi, certain VPNs).
Symptoms: `ping driveforge.local` doesn't resolve from your laptop
even though the daemon is clearly running.

Fallbacks:

- Use the **direct IP** shown in the boot banner (`/etc/issue`).
- Add a static `/etc/hosts` entry on your laptop:
  `10.10.10.166  driveforge.local`
- Configure your DHCP server to register the box's hostname in DNS
  (`option host-name` in `dhcpd.conf`, or pfSense's "Register DHCP
  leases in DNS" option).

The hostname rename helps mDNS clients (avahi, mDNSResponder on
Mac) but doesn't fix mDNS being blocked entirely.

## Validation reference

| Input | Result |
|-------|--------|
| `driveforge` | ✓ accepted |
| `forge-rack-b` | ✓ accepted |
| `node1` | ✓ accepted |
| `  ForgeRack  ` | ✓ accepted as `forgerack` (whitespace stripped, lowercased) |
| `` (empty) | ✗ Hostname is required |
| `123` (all digits) | ✗ Hostname must not be all digits |
| `-leading` | ✗ must not start or end with hyphen |
| `trailing-` | ✗ must not start or end with hyphen |
| `has space` | ✗ letters, digits, hyphens only |
| `has_underscore` | ✗ letters, digits, hyphens only |
| `has.dot` | ✗ single-label only (no FQDNs here) |
| `localhost` | ✗ reserved name |
| `<64-char string>` | ✗ Hostname must be 63 characters or fewer |
