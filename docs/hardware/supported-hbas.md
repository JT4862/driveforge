---
title: Supported HBAs
---

# Supported HBAs

DriveForge needs raw block-level access to drives — SMART data,
secure-erase commands, sg_format. That means the HBA must operate
in **IT mode** (Initiator-Target, pass-through), NOT RAID mode.

## TL;DR

- **LSI 9200-series, 9207-8i, 9300-series in IT mode** — fully
  supported, both SATA and SAS, all phases.
- **LSI SAS2308** (NX-3200, similar) — fully supported as of
  **v0.3.0** (SAT passthrough). Pre-v0.3.0 hit `CONFIG_IDE_TASK_IOCTL`
  on SATA drives.
- **Direct motherboard SATA** — works via Linux `libata`'s SAT shim.
- **Hardware RAID controllers** (PERC H710 stock, MegaRAID, Smart
  Array) — **not supported**. Crossflash to IT mode.
- **USB-SATA bridges** — refused for safety (likely external boot
  drives, not test targets).

## Per-HBA notes

### LSI 9200 / 9207-8i (IT-mode firmware)

The DriveForge reference platform. R720 LFF + crossflashed PERC H710
runs this. Every code path — SAS, SATA, NVMe (via M.2 / U.2 risers),
SES, sg_format, SAT passthrough — is regularly exercised here.

If you have a stock Dell PERC H710 (or H710P, H710 Mini, H310, etc.),
you'll need to crossflash to LSI IT-mode firmware. The
[fohdeesha PERC crossflash guide](https://fohdeesha.com/docs/perc.html)
is the canonical reference; it's a multi-step process involving
booting a special USB image and reflashing the card. Not reversible
without specialized tools, so commit before you start.

### LSI SAS2308 (NX-3200)

Used in Supermicro / Nutanix NX-series chassis (NX-3200, NX-3050,
similar). Slightly newer than the 9207-8i (same chip family, newer
firmware revision).

**Pre-v0.3.0 issue:** SATA secure-erase via `hdparm --security-erase`
fails with `CONFIG_IDE_TASK_IOCTL` because modern Debian kernels
removed the `HDIO_DRIVE_TASKFILE` ioctl that hdparm uses for
SATA-on-SAS pass-through.

**v0.3.0 fix:** SAT passthrough — same `SECURITY ERASE UNIT` ATA
command wrapped in a SCSI `ATA-PASS-THROUGH(16)` CDB, submitted via
`sg_raw`. Works universally. See
[Reference → SAT passthrough](../reference/api.md) and the v0.3.0
release notes for detail.

If you're running v0.3.0+, the NX-3200 is fully supported. If you're
on an older release and seeing erase failures, upgrade.

### LSI 9300 / 9305 / SAS3008 (newer)

Should work via the same SAT passthrough path that v0.3.0 ships.
Not regularly tested — if you run on these, hit the
[issue tracker](https://github.com/JT4862/driveforge/issues) to
share results.

### Direct motherboard SATA

Linux's `libata` driver implements an internal SAT layer that wraps
ATA commands in SCSI `ATA-PASS-THROUGH` for the kernel's SCSI mid-
layer. The same `sg_raw` commands DriveForge uses for SAS-attached
SATA drives also work for motherboard-attached SATA drives.

No special configuration required. Works on consumer chipsets
(Intel, AMD), enterprise platform chipsets (Intel C-series), and
add-in SATA cards (most ASMedia / Marvell 88SE-series).

### Adaptec / Areca / Microsemi SAS HBAs

Theoretically work — SAT-3 conformance has been mandatory for
SAS-family HBAs since 2008, so the ATA-PASS-THROUGH(16) CDB shape
is standardized.

Not regularly tested. If you have one, hit the issue tracker.

## Why HBAs and not RAID controllers

DriveForge needs:

- **SMART access** to read drive health attributes. RAID controllers
  abstract individual drives behind logical volumes — SMART data
  for the underlying drives often isn't passed through, or only
  partially.
- **Secure erase commands** via the drive's native security
  interface. RAID controllers intercept these.
- **`sg_format`** for SCSI FORMAT UNIT on SAS drives. Same problem.
- **Raw `lsblk` device names** like `/dev/sdX`. RAID controllers
  expose virtual disks, not physical ones.

Crossflashing a RAID controller to IT mode (where supported) gives
you a real HBA. PERC H710 → LSI 9207-8i is the most common path on
the homelab Dell hardware DriveForge targets.

## Why USB drives are refused

Looking at `driveforge/core/erase.py:secure_erase()`:

```python
if drive.transport == Transport.USB:
    raise PipelineFailure("secure_erase",
                          "refusing to erase USB-transport drive")
```

USB-attached drives are likely external boot drives, OS install
USB sticks, backup drives, etc. — things you very much don't want
DriveForge to wipe by accident. Hard refusal is safer than any
heuristic.

If you have a legit reason to test a USB drive (USB-to-SATA
adapter on a SATA drive you actually want erased), the workaround
is to remove the USB middleman: connect the drive directly to a
SATA or SAS port.

## Why hardware RAID isn't on the roadmap

DriveForge is a refurbishment tool — it operates on individual
drives one at a time. Hardware RAID is fundamentally about hiding
individual drives behind a logical-volume abstraction. The two
philosophies don't compose.

If your environment is RAID-only, you can:

- Pull drives out of RAID volumes one at a time and test them on a
  separate IT-mode HBA
- Run DriveForge in a separate refurbishment workflow station
- Crossflash one of your existing RAID controllers to IT mode if
  the model supports it

## Testing your HBA before committing

Before you flash a release ISO and rely on DriveForge:

```bash
# Confirm the HBA is in IT mode (not RAID mode)
lspci -v | grep -A 5 "SAS\|SCSI"

# Confirm individual drives are visible (not virtual disks)
lsblk
ls /sys/class/sas_device/      # SAS layer info

# Confirm SMART works
sudo smartctl -a /dev/sda

# Confirm SAT passthrough works for SATA drives behind SAS
sudo sg_raw /dev/sda 85 06 06 00 00 00 00 00 00 00 00 00 00 40 EC 00 \
  -r 512 -o /tmp/identify.bin
# (issues IDENTIFY DEVICE via SAT — should return 512 bytes if SAT works)
```

If `lsblk` shows logical volumes instead of physical drives, you're
in RAID mode and DriveForge won't work without a re-flash.
