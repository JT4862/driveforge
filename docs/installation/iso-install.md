---
title: ISO install
---

# ISO install

The recommended path. Flash the release ISO to a USB stick, boot the
target server, **pick one of two boot-menu entries** (standalone/operator
or agent), select the OS disk, walk away. The unattended portion takes
~10–15 minutes; you come back to a working DriveForge instance.

**Two install flavors** ship in the same ISO — you choose at boot time:

| Boot entry | What you get |
|---|---|
| **DriveForge** (default) | Standalone / operator install. First boot runs the setup wizard where you pick Standalone or Operator. |
| **DriveForge Agent** | Headless worker that joins an existing fleet. No wizard — the box comes up advertising itself on the LAN, and an operator on your network enrolls it with one click from Settings → Agents. Use for the 2nd, 3rd, Nth DriveForge box when you already have an operator running. |
| Manual Debian install | Fallback for operators who want a stock Debian box without DriveForge's preseed (rare). |

For fleet setup, see [Fleet mode](../operations/fleet.md) once both
boxes are installed.

## 1. Download + verify the ISO

Grab the latest release from
[GitHub Releases](https://github.com/JT4862/driveforge/releases/latest).
Each release attaches `driveforge-installer-X.Y.Z-amd64.iso` and a
detached `driveforge-installer-X.Y.Z-amd64.iso.sha256` checksum file.

Verify the download before flashing:

```bash
shasum -a 256 driveforge-installer-X.Y.Z-amd64.iso
# compare to the SHA in the release notes (or use the .sha256 file directly)
sha256sum -c driveforge-installer-X.Y.Z-amd64.iso.sha256
```

If the hash doesn't match, **don't flash it.** Re-download, and if the
mismatch persists, open an issue.

## 2. Flash to a USB stick

The ISO is hybrid (boots on both BIOS and UEFI). Any 1 GB+ stick works;
the ISO itself is ~830 MB.

### macOS

```bash
diskutil list                                 # find your USB device, e.g. /dev/disk5
diskutil unmountDisk /dev/diskN
sudo dd if=driveforge-installer-X.Y.Z-amd64.iso of=/dev/rdiskN bs=4m status=progress
sudo diskutil eject /dev/diskN
```

The `r` in `/dev/rdiskN` is the raw device node — much faster than
`/dev/diskN`. Replace `N` with your actual USB stick number.

### Linux

```bash
lsblk                                          # find your USB device, e.g. /dev/sdX
sudo dd if=driveforge-installer-X.Y.Z-amd64.iso of=/dev/sdX bs=4M status=progress oflag=direct
sync
```

### Windows

Use [Rufus](https://rufus.ie/) or [balenaEtcher](https://etcher.balena.io/).
Select the ISO, select the USB stick, hit Start. Either tool handles
the hybrid-ISO boot record correctly.

## 3. Boot the target

### Pick the right boot-menu entry

When the ISO boots, the menu shows:

```
DriveForge (standalone / operator)
DriveForge Agent (auto-join fleet on this network)
Manual Debian install (no preseed)
```

**First DriveForge box on your network?** Pick the default
"DriveForge" entry. The setup wizard will let you choose Standalone
or Operator on first boot.

**Adding to an existing fleet?** Pick "DriveForge Agent." No wizard,
no configuration — the box boots into candidate mode and waits for
your operator to adopt it. (Operators adopt by clicking Enroll on
Settings → Agents; see [Fleet mode](../operations/fleet.md) for
the full flow.)

### Boot order

Most servers have a one-time boot menu — press `F11` (Dell), `F12`
(HP), `F8` (Supermicro), or check your motherboard manual. Choose the
USB stick.

If you can't get to a one-time menu, change persistent boot order in
BIOS to put USB ahead of the hard drive, install, then revert.

### Dell iDRAC virtual media

R720 / R730 owners can skip the USB stick entirely:

1. iDRAC web UI → **Virtual Media** → **Connect Virtual Media**
2. **Virtual Media** → **Map CD/DVD** → upload the ISO
3. **Server Control** → **Virtual Console**
4. **Boot Controls** → **Boot Once** → **Virtual CD/DVD**
5. **Power Control** → **Reset System (warm boot)**

The ISO boots inside the virtual console. Disconnect virtual media
once the install completes.

### NX-3200

Plug the USB stick into a rear USB port (front USB on some SKUs is
flaky). Hit `F8` at the Supermicro splash screen for the boot menu.
Select the USB device — usually labeled with the brand of your stick.

## 4. Walk through the installer

The installer is **mostly unattended** but has one intentional
interactive step: **disk selection**.

### Disk selection (the only interactive step)

The installer pauses at "Select disk to install on" and shows every
detected drive. **Pick your boot drive** — the small internal SSD,
or whatever you've designated as the OS disk. **Do NOT pick a
front-bay drive** — those are reserved for drives under test, and
the installer will erase whatever you pick.

This step is intentionally manual. Auto-picking would risk overwriting
a drive-under-test target on a multi-bay rig. Once confirmed,
everything else is automated.

### What happens during the unattended portion

1. Debian 12 base system installs from the netinst (~5 min, downloads
   from `deb.debian.org`)
2. `git clone` of the DriveForge repo into `/opt/driveforge-src`
3. `scripts/install.sh` runs — installs `smartmontools`, `hdparm`,
   `sg3-utils`, `nvme-cli`, `ipmitool`, `avahi-daemon`, `ledmon`,
   etc.
4. Creates the `driveforge` system user
5. Installs systemd units (`driveforge-daemon.service`,
   `driveforge-issue.service`, `driveforge-update.service`)
6. Installs the `/etc/polkit-1/rules.d/50-driveforge-update.rules`
   polkit rule that authorizes one-click in-app updates (v0.6.0+;
   pre-v0.6.0 used a sudoers rule)
7. Reboots into the installed system

If any step fails, look at `/var/log/driveforge-install.log` on the
installed system (the installer leaves it behind for diagnosis).

## 5. First boot

After the reboot, you'll see the **TTY login banner** at the console:

```
  DriveForge on driveforge (kernel 6.1.0-...)

  Dashboard:
    → http://driveforge.local:8080     (preferred — mDNS)
    → http://10.10.10.166:8080         (direct IP)

  Admin SSH:
    → ssh forge@driveforge.local
    → ssh forge@10.10.10.166
```

The banner is dynamically populated each boot — the IP reflects what
DHCP actually handed out.

### Default credentials

The preseed creates one user:

| Account | Password | Notes |
|---------|----------|-------|
| `forge` | `driveforge` | Admin user, sudo-enabled. **Change immediately:** `passwd` |
| `root` | (locked) | Root login is disabled. Use `sudo` from the `forge` account. |

The `driveforge` system account that runs the daemon has no password —
it's locked, only used for daemon process isolation.

### Open the dashboard

Point a browser at `http://driveforge.local:8080`. If `.local`
resolution doesn't work on your network (corporate Wi-Fi sometimes
blocks mDNS), use the direct IP from the banner.

You'll be redirected to **Setup wizard step 1**.

## 6. Walk through the setup wizard

Three steps:

1. **Welcome / chassis confirmation.** Confirms which physical
   chassis you're installing on so the dashboard knows whether to
   expect SES, IPMI, etc.
2. **Hardware capabilities + drives + network.** Shows what
   DriveForge auto-detected (SES, BMC, drives, network). You can
   override anything that looks wrong.
3. **Integrations + finish.** Optional: outbound webhook URL,
   Cloudflare Tunnel hostname. Then **Finish** — wizard closes,
   dashboard loads.

The wizard writes `/etc/driveforge/driveforge.yaml`. You can replay
it any time from **Settings → Advanced → Run setup wizard again**.

## 7. Test the install end-to-end

Drop a drive (anything you don't care about) into a front bay. Within
~3 seconds it should appear in the **Installed** section of the
dashboard. Click **+ New Batch**, select it, click **Start**, watch
the pipeline run.

If the drive doesn't appear: check `journalctl -u driveforge-daemon -f`
for hotplug errors.

## Next steps

- [Dashboard tour](../operations/dashboard-tour.md) — what every UI
  element means
- [Auto-enroll](../operations/auto-enroll.md) — turn on hands-off
  pipeline-on-insert
- [Settings → Hostname](../operations/hostname-rename.md) — rename
  the box if you have multiple DriveForges on the same LAN
- [In-app self-update](../operations/self-update.md) — keeping it
  current
