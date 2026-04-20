# DriveForge installer ISO

Self-contained Debian 12 + DriveForge installer ISO. `dd` it to a USB
stick, boot the target server, pick the OS disk at the partitioning
prompt — everything else is automated, including the offline DriveForge
install. Works without internet on the target.

## What's inside

| Path | Purpose |
|------|---------|
| `iso/preseed.cfg` | Unattended Debian 12 install configuration |
| `scripts/build-offline-bundle.sh` | Pre-downloads .deb + .whl files into `dist/driveforge-offline-<ver>.tar.gz` |
| `scripts/build-iso.sh` | Repacks the Debian netinst ISO with the preseed + offline bundle baked in |

## Why two scripts?

- **`build-offline-bundle.sh`** is useful on its own — you can `scp` the
  resulting tarball to any air-gapped Debian 12 box and run
  `sudo DRIVEFORGE_OFFLINE_BUNDLE="$(pwd)" ./scripts/install.sh`. No ISO
  needed.
- **`build-iso.sh`** wraps the bundle into a bootable installer for the
  zero-decision turnkey path.

## Building the ISO

Two paths — pick whichever matches where you are.

### Option A: Docker (recommended, works on macOS)

Requires Docker Desktop, Colima, or OrbStack — anywhere `docker info`
returns successfully. No Debian VM needed.

```bash
./scripts/build-iso-docker.sh
```

The wrapper builds a `debian:12-slim` container with `xorriso` +
`apt-utils` + `python3-pip` + `isolinux`, mounts the repo, and runs
`build-iso.sh` inside. Output lands in `dist/`.

On Apple Silicon the build runs under `linux/amd64` emulation (Rosetta)
so the resulting ISO targets x86 servers correctly. First build pulls
~150 MB of Docker layers + ~700 MB Debian netinst + ~300 MB of cached
debs/wheels — count on **10-15 min**. Subsequent builds reuse all the
caches and finish in **2-3 min**.

### Option B: Native Debian 12 host

If you already have a Debian 12 box (or your R720 itself when not busy):

```bash
sudo apt-get install -y xorriso curl tar isolinux
sudo ./scripts/build-iso.sh
```

Outputs `dist/driveforge-installer-<version>-amd64.iso` (~1 GB). Builds
in ~5 minutes the first time, faster afterwards.

## Smoke-testing in QEMU before flashing

You can boot the ISO in QEMU on macOS to verify the preseed flow without
burning a USB stick. One-time setup: `brew install qemu`.

```bash
qemu-img create -f qcow2 /tmp/test-disk.qcow2 20G

qemu-system-x86_64 \
  -m 4G -smp 2 -accel tcg \
  -drive file=dist/driveforge-installer-0.0.1-amd64.iso,format=raw,readonly=on,if=none,id=cdrom \
  -device ide-cd,drive=cdrom,bootindex=2 \
  -drive file=/tmp/test-disk.qcow2,if=none,id=disk0,format=qcow2 \
  -device virtio-blk-pci,drive=disk0,bootindex=1 \
  -netdev user,id=n0,hostfwd=tcp::8081-:8080,hostfwd=tcp::2222-:22 \
  -device virtio-net,netdev=n0 \
  -audiodev none,id=ad \
  -device intel-hda -device hda-duplex,audiodev=ad \
  -display vnc=:5,password=on \
  -monitor unix:/tmp/qemu-mon.sock,server,nowait \
  -daemonize -pidfile /tmp/qemu.pid
```

**Boot-order note**: the disk is `bootindex=1` and the CD is `bootindex=2`
on purpose. First boot, the empty qcow2 has no valid MBR so SeaBIOS
falls through to the CD and runs the installer. After install + reboot,
the disk now has GRUB → boots the installed Debian directly. If you
reverse these (CD=1, disk=2) you get an infinite install loop because
every reboot lands back on the installer ISO.

Set the VNC password (macOS Screen Sharing requires one):

```bash
python3 -c "
import socket, time
s = socket.socket(socket.AF_UNIX); s.connect('/tmp/qemu-mon.sock')
time.sleep(0.4); s.recv(2048)
s.send(b'set_password vnc forge\n'); time.sleep(0.4)
"
```

Connect from Mac: Finder → `Cmd+K` → `vnc://localhost:5905` → password `forge`.

The `-audiodev none -device intel-hda` bits are non-obvious but
required: Debian's installer auto-starts speech-synthesis after a
5-second timeout if no key is pressed, and without an audio device it
loops `No sound card detected after N seconds...` forever. The
null-backend HDA satisfies the probe so the installer proceeds.

After install, port-forwarding `8081 → 8080` lets you reach the
installed daemon at `http://localhost:8081`.

Under TCG emulation (any non-Intel Mac), expect 30-60 minutes for the
full unattended install to finish. Hardware boot is much faster.

## Flashing to USB

```bash
sudo dd if=dist/driveforge-installer-<version>-amd64.iso \
  of=/dev/sdX bs=4M status=progress conv=fsync
```

Replace `/dev/sdX` with your USB stick's device node. Check with
`lsblk` first — getting this wrong overwrites the wrong disk.

On macOS:
```bash
diskutil list                          # find the USB
diskutil unmountDisk /dev/diskN
sudo dd if=...iso of=/dev/rdiskN bs=4m
```

## Booting

The ISO is hybrid-bootable (BIOS + UEFI). Methods:

- **USB**: plug into target, boot, select USB from boot menu (F11/F12 on
  most servers; F11 on Dell PowerEdge)
- **iDRAC virtual media**: in Dell iDRAC, `Configuration → Virtual Media
  → Map CD/DVD → ISO Image File`, then boot the server with `F2 → Boot
  Manager → Virtual CD/DVD/ISO`
- **VM**: attach the ISO as a CD/DVD drive

## What happens during install

1. Debian installer boots, auto-loads `preseed.cfg`
2. Locale, keyboard, timezone, network, hostname (`driveforge`) all
   pre-answered — no prompts
3. **Partitioning pauses for operator input** — you must select the OS
   disk. *Critical safety: do not pick a front-bay drive.* On the R720
   the OS SSD is typically the smallest and on the rear/internal slot.
4. Debian base install runs, `tasksel standard` + `ssh-server`
5. `late_command` copies `/cdrom/driveforge-bundle/` to
   `/usr/local/share/driveforge-bundle/` and runs `install.sh` with
   `DRIVEFORGE_OFFLINE_BUNDLE` set so it uses the cached debs + wheels
6. Reboot — daemon starts on boot, dashboard reachable at
   `http://driveforge.local:8080` (or `http://<dhcp-ip>:8080`)

If the DriveForge step fails, the install log is at
`/var/log/driveforge-install.log` on the new system. Most common cause:
hardware compatibility issue (e.g. PERC not in IT mode → no drive
discovery). The base Debian install succeeds either way; you can rerun
`./install.sh` manually after fixing.

## Default credentials

The installer creates a login user `forge` with password `driveforge`.
**Change this immediately on first login** — the password is only meant
to get you in over SSH the first time. The DriveForge daemon runs as a
separate non-login system account, also named `driveforge`, that's NOT
the admin user.

## Updating an installed system

The ISO is for first-time install only. To update an existing
DriveForge installation:

```bash
ssh admin@driveforge.local
cd /usr/local/share/driveforge-bundle  # or wherever you cloned/extracted
git pull   # if you used git clone
sudo ./scripts/install.sh   # re-runs install on top of existing state
```

The install.sh is idempotent — it preserves the SQLite DB and your
`/etc/driveforge/*.yaml` config across re-runs.

## Known limitations

- **Partitioning is interactive** by design (refuses to auto-pick a disk
  that might be a drive-under-test target). If you want fully unattended
  installs across many machines with identical hardware, edit
  `iso/preseed.cfg` to set `partman-auto/disk` explicitly to your boot
  drive's expected device path.
- **Network during install is optional**. The bundled offline mode
  works without internet, but the Debian installer's network step still
  runs (for hostname/DHCP) — you'll get a "no mirror reachable" warning
  on a fully-isolated network, which is harmless. Press Continue.
- **Base Debian patches are frozen at the netinst version**. Run
  `sudo apt-get update && apt-get upgrade` after install for the latest
  security patches (this needs network).
