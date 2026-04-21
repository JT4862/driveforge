---
title: Supported drives
---

# Supported drives

What works, what doesn't, what's quirky.

## Quick reference

| Transport | Erase mechanism | Typical duration | Status |
|-----------|-----------------|------------------|--------|
| SATA HDD | SAT passthrough `SECURITY ERASE UNIT` (v0.3.0+) | Hours per TB (full overwrite) | ✓ Supported |
| SATA SSD | SAT passthrough `SECURITY ERASE UNIT` | Seconds (vendor crypto-erase, most enterprise SSDs) | ✓ Supported |
| SAS HDD | `sg_format --format` (SCSI FORMAT UNIT) | 15–60 min for 1 TB, scales linearly | ✓ Supported |
| SAS SSD | `sg_format --format` | Vendor-dependent; often crypto-erase | ✓ Supported |
| NVMe | `nvme format -s 1` | Seconds (crypto-erase) | ✓ Supported |
| USB-SATA | (refused) | — | ✗ Refused for safety |
| RAID virtual disk | (no path) | — | ✗ Not supported (use IT-mode HBA) |

## SATA drives

### HDDs

DriveForge issues ATA `SECURITY ERASE UNIT` via SAT passthrough
(`sg_raw` + ATA-PASS-THROUGH(16) CDB) since v0.3.0. The drive does
a full LBA-by-LBA overwrite internally.

**Duration scales with capacity.** Drive firmware reports an estimate
via `hdparm -I` (which still works for read-only identify; it's only
the legacy task ioctl that was removed). DriveForge reads that
estimate, multiplies by 1.5 for headroom, uses the result as the
SG_IO timeout. No hardcoded upper cap — an 8 TB drive that needs
40 hours gets 40 hours.

If the drive's firmware doesn't report an estimate (some older
drives don't), DriveForge falls back to a capacity-based heuristic:
20 min per 100 GB, clamped between 5 minutes and 6 hours.

### SSDs

Same code path as HDDs. The drive decides internally whether to do
a full overwrite or a vendor-specific crypto-erase. Most enterprise
SSDs (Intel DC, Samsung PM/SM, Micron 5/7/9-series) implement
SECURITY ERASE UNIT as a crypto-erase: rotate the internal media
key, all ciphertext on-disk becomes unrecoverable. Completes in
seconds.

DriveForge logs the actual wall-clock duration after secure_erase.
A sub-30-second completion gets a "(likely vendor crypto-erase —
data is still unrecoverable)" annotation in the log so the operator
doesn't think the phase was skipped.

Consumer SATA SSDs are mixed. Most modern ones (Samsung 8x0/9xx,
Crucial MX-series, WD Blue/Red) implement crypto-erase. Some
older / cheaper drives do a full overwrite. Both paths produce a
data-unrecoverable end state; only the duration differs.

## SAS drives

### HDDs

DriveForge issues `sg_format --format` (which under the hood is
SCSI FORMAT UNIT). The drive reformats every sector internally.

Duration: ~15–60 minutes for 300 GB SAS HDDs, scales roughly
linearly with capacity. A 4 TB SAS HDD takes 6–8 hours; a 16 TB
SAS HDD can take a full day or more.

The `sg_format` process is uninterruptible mid-flight — killing it
with SIGKILL leaves the drive in "Medium format corrupted" state
that requires another `sg_format`-to-completion to clear. This is
why the per-drive Abort button is **disabled** during the
`secure_erase` phase. See [Aborting drives](../operations/aborting-drives.md).

### SSDs

SAS SSDs (Seagate Nytro, HGST Ultrastar SS-series, Toshiba PX-series)
respond to `sg_format` per the SCSI Block Commands spec. Most
enterprise SAS SSDs implement this as a vendor crypto-erase that
completes in seconds; a few older ones do a full overwrite (minutes
to hours depending on capacity).

## NVMe drives

`nvme format -s 1 -f /dev/nvmeXnY`:

- `-s 1` = User-data crypto-erase (the default modern action)
- `-f` = Force, suppress prompts

Completes in seconds even on multi-TB NVMe drives. The flat 1-hour
timeout is generous; a stuck format that long would indicate
firmware misbehavior, not normal operation.

DriveForge's NVMe path doesn't currently issue `nvme sanitize`
(the SANITIZE BLOCK ERASE command) as an alternative — `format -s
1` is sufficient for the data-erasure goal and supported by every
NVMe drive made.

## Self-encrypting drives (SED / Opal)

Currently treated as plain drives. The SECURITY ERASE UNIT (SATA),
SCSI FORMAT UNIT (SAS), or NVMe FORMAT path runs unchanged — which,
on most SED drives, triggers the vendor's crypto-erase fast path.

A future "SED-aware mode" that explicitly uses TCG Opal commands
(`sedutil-cli psid revert` for unknown PIN drives, etc.) is on the
backlog but not in v0.4.0. If you have an SED drive that doesn't
respond well to the standard erase path, file an issue.

## Drives with HPA / DCO set

Host-Protected Area (HPA) and Device Configuration Overlay (DCO)
are mechanisms that hide a portion of the drive from the OS — used
by some vendors to reserve space for diagnostic partitions, factory
recovery images, etc.

**Current DriveForge behavior:** the HPA / DCO is left intact. The
visible portion of the drive is what gets erased.

A future "wipe HPA + DCO too" option is on the backlog. The use
case is forensic-grade sanitization where the hidden area might
contain residual data. Most refurb workflows don't need this.

## SMART-unhealthy drives at enrollment

The pre-test SMART check captures a snapshot of the drive's health
before any erase or burn-in runs. If the snapshot reports
`smart_status_passed=False` (the drive itself signals "I'm
failing"), the pipeline still runs through every phase — but the
final grading sees the bad SMART status and assigns Fail.

Pre-emptively rejecting SMART-unhealthy drives at enrollment was
considered and rejected: an operator might want to confirm a
suspect drive's failure with a full burn-in pass, OR test whether
a drive's SMART has self-cleared after a power cycle. The
permissive default lets both workflows happen; the grading layer
catches the verdict.

## USB-attached drives

```python
if drive.transport == Transport.USB:
    raise PipelineFailure("secure_erase",
                          "refusing to erase USB-transport drive")
```

Hard refusal at the secure-erase dispatch. Rationale in
[Supported HBAs](supported-hbas.md). To test a SATA drive
currently attached via USB, plug it into a real SATA / SAS port
first.

## What "supported" means here

Drive support means **the secure-erase + burn-in + grade pipeline
runs end-to-end**. It does NOT mean DriveForge has a database of
every drive model + firmware combination ever shipped. Vendors
occasionally ship firmware that handles standard ATA / SCSI /
NVMe commands in non-standard ways; DriveForge usually surfaces
those as failed runs with the underlying tool's error message.
File an issue with the drive model + firmware version + log
output if you hit one.

## What's tested in CI vs in production

CI (the test suite running on every PR + commit) uses **recorded
fixtures** of `smartctl`, `nvme-cli`, `ipmitool`, `sg_format`, etc.
output. No real drives are involved. This means the orchestrator
logic is well-tested for the captured drive models, but a brand-
new drive model might surface fixture-coverage gaps.

Production use (R720 + NX-3200) exercises real hardware against
real drives. The `Z1F248SL` Seagate ST3000DM001 and similar units
are the real-world test cases for the SAT passthrough path.
