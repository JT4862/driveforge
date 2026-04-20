# Installing DriveForge

Two supported install paths. Pick the one that matches your situation.

| Path | Good for | Effort |
|---|---|---|
| **[A. ISO installer (recommended)](#path-a-iso-installer-recommended)** | Fresh hardware with no OS yet. One USB stick, one boot, done. | ~15 min wall-clock, mostly unattended |
| **[B. Existing Debian 12 server](#path-b-existing-debian-12-server)** | Hardware that already runs Debian 12 (or your own Bookworm VM) | ~5 min once SSHed in |

Both paths end in exactly the same state: `driveforge-daemon` running,
dashboard reachable at `http://driveforge.local:8080`.

---

## Hardware prerequisites

### Recommended (what DriveForge is built around)

- **Server**: Dell PowerEdge R720 (LFF or SFF variant), R720xd, R730,
  Supermicro/Nutanix chassis (NX-3200 etc.), or any 2U rackmount with
  a SAS backplane
- **HBA**: LSI 9200-series / 9207-8i in **IT mode**. Dell PERC H710
  cards need to be crossflashed — see the
  [fohdeesha PERC crossflash guide](https://fohdeesha.com/docs/perc.html).
  Stock RAID firmware **will not work** — DriveForge needs raw
  pass-through for SMART + sg_format.
- **Boot drive**: small (≥ 120 GB) SSD on an **internal/rear** slot
  (motherboard SATA, internal USB, rear flex bay). **Never boot from a
  front drive bay** — those are reserved for drives under test.
- **Network**: wired Ethernet on the same LAN as the machine you'll use
  to manage it

### Minimum viable (for anyone without enterprise hardware)

- Any x86_64 system with 4+ SATA/SAS bays
- Any HBA in IT mode (or direct-attach SATA on motherboard)
- Boot drive separate from the bays you'll test
- Without a SES-capable backplane, DriveForge falls back to a
  configurable virtual-bay count (default 8)

### Optional

- **JBOD expansion** (e.g. Dell MD1200, Supermicro SC846) — auto-detected
  as additional SES enclosures, dashboard expands automatically
- **iDRAC / IPMI** — chassis power telemetry in the UI
- **Thermal printer** — any Brother QL-family (QL-800 / QL-810W /
  QL-820NWBc / QL-1100 / QL-1110NWBc) for adhesive cert labels.
  DK-1209 29×62 mm labels are the recommended roll for 3.5" HDDs.

---

## Path A: ISO installer (recommended)

The DriveForge ISO is a Debian 12 netinst with our preseed layered on
top. One USB, one boot, one manual confirmation of the OS disk at
partitioning — everything else runs unattended.

**The target server needs internet during install.** The ISO only
contains the Debian installer + DriveForge preseed; during the
`late_command` step, the installer:

1. Reaches Debian's mirrors for the base system packages
2. `git clone`s DriveForge from the public GitHub repo
3. Runs `./scripts/install.sh` which apt-installs DriveForge's runtime
   deps and pip-installs the app

If you're setting up on an air-gapped network, see
[Path C (Air-gapped install)](#path-c-air-gapped-install) below.

### 1. Download the ISO

Grab the latest release ISO from the
[GitHub Releases page](https://github.com/JT4862/driveforge/releases/latest).
Look for `driveforge-installer-<version>-amd64.iso` (~945 MB).

Verify the SHA-256 checksum against the release page before flashing —
the release notes list the expected sum.

### 2. Flash to a USB stick

The ISO is hybrid BIOS+UEFI bootable. 8 GB+ USB stick. The stick's
current contents will be wiped.

**On macOS**:
```bash
diskutil list                                  # find the USB — note its diskN
diskutil unmountDisk /dev/diskN
sudo dd if=driveforge-installer-<version>-amd64.iso of=/dev/rdiskN bs=4m status=progress
```
(`rdiskN` is the raw device — much faster than `diskN` on macOS.)

**On Linux**:
```bash
lsblk                                          # find the USB — note its /dev/sdX
sudo dd if=driveforge-installer-<version>-amd64.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

**On Windows**: [Rufus](https://rufus.ie) or [balenaEtcher](https://www.balena.io/etcher/).

### 3. Boot the target server from the USB

- **Physical USB**: plug in, reboot, hit the boot-menu key (F11 on Dell
  PowerEdge, F12 on some others), pick the USB
- **iDRAC / IPMI virtual media**: Configuration → Virtual Media → Map
  CD/DVD → point at the ISO file → F2 Boot Manager → "Virtual
  CD/DVD/ISO"

### 4. Let the installer run

You'll see a boot prompt briefly ("DriveForge installer — auto-firing in
5s"), then the installer starts automatically. Everything is preseeded
**except** the partitioning step, which intentionally pauses so you
choose the install disk.

**At the partitioning screen**: pick the OS drive — usually the smallest
drive and **never** a front-bay drive you want to test. Confirm the
wipe.

After that, the install runs unattended for 10–30 minutes depending on
hardware (5–10 min on a fast SSD + modern CPU; 20–30 min on slower
hardware). The server will reboot itself when done.

### 5. First boot

After reboot, the server boots Debian from the installed disk (no need
for the USB anymore — pull it if you like). Default credentials:

- **Username**: `forge`
- **Password**: `driveforge`

**Change the password immediately** — those defaults are setup-only:

```bash
ssh forge@driveforge.local       # or by IP if mDNS isn't working
passwd                           # set a real password
```

The DriveForge daemon is already running. Skip to
["First-run setup wizard"](#first-run-setup-wizard).

---

## Path B: Existing Debian 12 server

If you already have a Debian 12 Bookworm server (physical, VM, or
otherwise) — clone the repo and run the installer.

### Prerequisites

- Debian 12 Bookworm (other distros unsupported, Trixie untested)
- Root access via `sudo`
- Network access (internet for apt + pip, LAN for UI access later)
- SSH access from your workstation

### Install

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/JT4862/driveforge.git
cd driveforge
sudo ./scripts/install.sh
```

The installer:

- Installs system dependencies (smartmontools, hdparm, sg3-utils,
  nvme-cli, e2fsprogs, fio, tmux, ipmitool, avahi-daemon,
  python3-venv, etc.)
- Creates the `driveforge` system user + `/var/lib/driveforge` state
  directory
- Creates a Python venv at `/opt/driveforge` and installs the DriveForge
  package (plus the `linux` extra for `pyudev` hotplug support)
- Symlinks `/usr/bin/driveforge{,-daemon,-tui}`
- Writes default config to `/etc/driveforge/`
- Installs + enables the `driveforge-daemon.service` systemd unit
- Ensures `avahi-daemon` is running so `driveforge.local` resolves

If you see:

```
✓ DriveForge installed and running.

Open the web UI at:
  → http://driveforge.local:8080     (mDNS, preferred)
  → http://<server-ip>:8080          (direct IP)
```

You're ready.

---

## Path C: Air-gapped install

For environments where the DriveForge server cannot reach the internet
during installation. Requires an internet-connected staging machine
(your laptop works) and a way to shuttle files to the target — USB
stick, internal network, whatever you have.

### 1. On the internet-connected staging machine

Clone DriveForge and pre-download its Debian package dependencies +
Python wheels using the supplied bundler script:

```bash
git clone https://github.com/JT4862/driveforge.git
cd driveforge
sudo ./scripts/build-offline-bundle.sh
```

This produces a single `dist/driveforge-offline-<version>.tar.gz`
(~113 MB) containing:

- The DriveForge source tree (git archive of HEAD)
- All required `.deb` packages pre-resolved and downloaded
- All Python wheels (including a pre-built `driveforge-*.whl`)

Copy the tarball to a USB stick or somewhere the air-gapped machine
can reach.

### 2. Install Debian 12 on the target the normal way

Use the standard Debian 12 netinst ISO from
[debian.org](https://www.debian.org/distrib/netinst) (NOT the
DriveForge ISO — that one assumes internet during install).

Install Debian as usual, picking **SSH server + standard system
utilities** at the tasksel step. Set hostname to `driveforge` so mDNS
works later. The Debian installer itself can run from the netinst
ISO's own package set without a mirror — pick "Do not use a network
mirror" when prompted.

### 3. On the target, extract + install from the bundle

Copy the tarball over (USB, local network, however you're doing it),
then:

```bash
# As root on the target:
cd /tmp
tar xzf driveforge-offline-<version>.tar.gz
cd driveforge-offline-<version>
sudo DRIVEFORGE_OFFLINE_BUNDLE="$(pwd)" ./scripts/install.sh
```

The `DRIVEFORGE_OFFLINE_BUNDLE` env var tells `install.sh` to resolve
.debs and Python wheels from the bundle instead of reaching out to
Debian mirrors and PyPI.

If the target has sporadic internet (behind a firewall, NAT, etc.),
simpler option: use [Path A](#path-a-iso-installer-recommended) and
let whatever intermittent connectivity it has fetch packages during
install.

---

## (Optional, both paths) Set a static IP

DriveForge works fine on DHCP, but a static IP means `http://<ip>:8080`
stays stable across reboots. Debian 12 uses netplan by default:

```bash
sudo nano /etc/netplan/01-driveforge.yaml
```

Minimal example (adjust for your network):

```yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    eno1:                          # your actual NIC name from `ip link`
      dhcp4: no
      addresses: [192.168.1.50/24]
      routes:
        - to: default
          via: 192.168.1.1
      nameservers:
        addresses: [1.1.1.1, 192.168.1.1]
```

Apply:

```bash
sudo netplan apply
```

mDNS (`driveforge.local`) works regardless.

---

## First-run setup wizard

Open `http://driveforge.local:8080` in a browser. A five-step wizard
walks you through:

1. **Welcome** — overview and what the wizard will cover
2. **Hardware & network** — read-only report of detected drives, SES
   enclosures, network state, IPMI availability. Confirm things look
   right, click Next.
3. **Printer** — pick a Brother QL model + label roll, or skip. If you
   plug in the printer later, udev auto-configures it.
4. **Grading thresholds** — A/B/C/Fail rules. Defaults are conservative
   and fine for most homelab use; revisit later under Settings →
   Grading.
5. **Integrations** — outbound webhook URL + optional Cloudflare Tunnel
   hostname. Both skippable.

After Finish, you land on the dashboard.

---

## Running your first batch

1. Plug drives into the front bays (hot-plug works — they appear on the
   dashboard within a few seconds)
2. Click **+ New Batch**, optionally name the source, review the
   selected drives, type **`ERASE`** in the confirmation box, click
   **Start Batch**
3. Walk away. Per-drive runs take:
   - **Quick mode** (skip badblocks + long self-test): ~5 min on SSD,
     ~30 min on a typical 1 TB HDD, a few hours on 8 TB+
   - **Full mode** (8-pass badblocks + long test): ~1 day per TB on HDDs
4. Monitor the dashboard whenever you like — it polls every 3 s

When a batch completes:
- Each passing drive gets a printed cert label (if a printer is
  configured) via the batch detail page's "Print all passing" button
- The outbound webhook (if configured) fires once with the batch summary

---

## Troubleshooting

### `driveforge-daemon` won't start

```bash
sudo journalctl -u driveforge-daemon -n 50 --no-pager
```

Common causes:
- `/etc/driveforge/` not writable by the `driveforge` user (should be
  fixed automatically by `install.sh` — re-run if things drift)
- Port 8080 already in use
- Corrupted venv — remove `/opt/driveforge` and re-run `install.sh`

### Can't reach `http://driveforge.local:8080`

- Check avahi: `systemctl status avahi-daemon`
- Check the daemon is listening: `sudo ss -tlnp | grep 8080`
- Windows: install Apple Bonjour for `.local` resolution
- Fall back to the raw IP: `http://<server-ip>:8080`

### No drives detected on the dashboard

- Confirm HBA in IT mode: `lspci | grep -i lsi` should show a `SAS23xx`
  or `9207`-family chip, **not** `MegaRAID`
- `sudo smartctl --scan` — should list every drive
- `lsblk` — check the kernel sees the block devices

### SES enclosure not detected

- Check `ls /sys/class/enclosure/`. Empty = your backplane doesn't have
  an enclosure processor (R720 LFF direct-attach is one such case).
  DriveForge falls back to the virtual-bay count from Settings → Daemon.
  Adjust if the default (8) doesn't match your chassis.

### Printer not auto-detecting

- Brother QLs use USB VID `0x04f9`. Check `lsusb | grep 04f9`
- If the printer is connected but DriveForge doesn't see it, restart the
  daemon: `sudo systemctl restart driveforge-daemon`

---

## Next steps

- [UPDATE.md](UPDATE.md) — how to keep DriveForge current once it's
  installed
- [BUILD.md](BUILD.md) — architecture + design notes
- [CONTRIBUTING.md](CONTRIBUTING.md) — how this project works with
  issues / PRs / forks
