# DriveForge

**In-house enterprise drive refurbishment pipeline.**

DriveForge turns a dedicated Debian server (typically a Dell PowerEdge
R720) into an automated drive testing, grading, and certification rig.
Pulled enterprise drives go in; SMART-validated, secure-erased,
burned-in, graded, cert-labeled drives come out — ready for the homelab.

- **Status**: pre-alpha, in active development
- **License**: [MIT](LICENSE)
- **Latest release**: [GitHub Releases](https://github.com/JT4862/driveforge/releases/latest)

> **Warning — drive-destructive software.** DriveForge secure-erases
> every drive it tests. The OS disk is excluded automatically, but do
> not plug any drive you want to keep into a test bay until you
> understand the workflow.

## Docs

- **[INSTALL.md](INSTALL.md)** — getting DriveForge onto your hardware
  (ISO path + manual Debian path)
- **[UPDATE.md](UPDATE.md)** — keeping it current once installed (four
  update lanes)
- **[BUILD.md](BUILD.md)** — architecture + design decisions
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — how this project handles
  issues, PRs, and forks
- **[SECURITY.md](SECURITY.md)** — reporting vulnerabilities

---

## What it looks like

![DriveForge dashboard — live bay grid showing tested drives with grades](docs/screenshots/dashboard.png)

The **dashboard** is the home screen — one card per physical drive bay,
grouped by SAS enclosure (or falling back to virtual bays on
direct-attach backplanes). Each card shows at a glance whether the
drive is empty, installed-idle, actively testing, or carries a previous
test result (pass / quick-mode pass / fail).

![Drive detail page — full test-run history, SMART attributes, thermal chart, phase log](docs/screenshots/drive-detail.png)

Click a bay card to see **full drive detail**: grade + rationale,
suggested use tier, SMART attributes, test duration, temperature during
test, phase-by-phase log output, hardware info, and full test history
across every batch the drive has ever been in.

![Public cert report — QR-coded, no-login, grading rationale included](docs/screenshots/report.png)

Each completed drive gets a **public cert page** at `/reports/<serial>`
— the target of the QR code on the printed label. No login required.
Shows grade, rationale, SMART attrs, and test date. Quick-mode results
are clearly marked as provisional. Exposable externally via Cloudflare
Tunnel.

![New batch form — drive selection, quick-mode toggle, type-ERASE confirm gate](docs/screenshots/new-batch.png)

Starting a batch requires typing **ERASE** to confirm — every drive you
select will be secure-erased. Quick mode (skip badblocks + long
self-test) is a checkbox for faster turnaround on drives you don't need
a full certification for.

---

## What it does

1. **Auto-discovers drives** on plug-in (udev hotplug) and the attached
   SAS enclosure(s) via SES
2. **Per-drive pipeline** in parallel (up to 8 drives on an R720; more
   with JBOD expansion):
   - Pre-test SMART baseline
   - SMART short self-test
   - Firmware version logged (manual updates only)
   - Secure erase (SATA `hdparm` / SAS `sg_format` / NVMe `nvme format`)
   - `badblocks` destructive write/read (skipped in quick mode)
   - SMART long self-test (skipped in quick mode)
   - Post-test SMART diff → grade (A / B / C / fail) with per-rule
     rationale
   - Optional outbound webhook (n8n / Zapier / any HTTPS endpoint)
3. **Cert labels** printed on-demand from the batch or drive detail
   page — not automatically, so label stock doesn't get wasted on
   failed drives and you can reprint if a sticker peels
4. **Local web UI** at `http://driveforge.local:8080` — live bay state,
   per-drive SMART history, telemetry charts, and the public QR-coded
   report page for each completed drive
5. **Post-run LED signaling** — after a drive finishes, its bay's green
   activity LED goes solid (pass) or lighthouse-blinks (fail) so you
   can see what to pull from across the room. On hardware with proper
   SES backplanes, the amber fault LED also lights for failures.

---

## Sanitization standard

DriveForge's erase pipeline meets **NIST SP 800-88 Rev. 1 — Purge**,
the current authoritative media-sanitization standard (which
superseded DoD 5220.22-M for media sanitization in 2007). The pipeline
is two stacked phases, both of which happen on every drive:

**Phase 1 — Drive-level secure erase** (firmware-initiated, always runs):

| Transport | Command | Mechanism |
|-----------|---------|-----------|
| SATA | `hdparm --security-erase` | ATA SECURITY ERASE UNIT |
| SAS | `sg_format --format` | SCSI FORMAT UNIT |
| NVMe | `nvme format` | NVMe Format NVM (crypto or user-data erase) |

On SSDs this rotates the internal encryption key or block-erases the
entire flash **including over-provisioned reserve blocks that the host
cannot address** — data the host OS has no way to reach on its own. On
HDDs it's a vendor-firmware full-surface sanitize.

**Phase 2 — Verified overwrite** (full mode only; skipped in quick mode):

Four full-drive write/read passes via `badblocks -wsv` using patterns
`0xAA → 0x55 → 0xFF → 0x00`. Every sector is written with each pattern
then read back and verified. Unrecoverable errors feed into the grade.

### How this compares

| Standard | Requirement | DriveForge |
|----------|-------------|------------|
| **NIST 800-88 Clear** | 1 overwrite pass | ✓ satisfied by any single badblocks pattern |
| **NIST 800-88 Purge** | Crypto-erase *or* firmware secure erase | ✓ Phase 1 (both quick and full mode) |
| DoD 5220.22-M (deprecated) | 3 overwrite passes | ✓ exceeded — full mode runs 4 verified passes |
| DoD 5220.22-M ECE (deprecated) | 7 overwrite passes | ✗ not matched by pass count, but Phase 1 + Phase 2 exceeds the intent |

Each cert report page (`/reports/<serial>`) includes a **Sanitization**
section spelling out which phases ran for that specific drive.
Quick-mode results are clearly marked: Purge compliance is intact, but
the 4-pass verification was skipped — re-run in full mode for a full
cert.

---

## Firmware update limitations

DriveForge **logs drive firmware versions** but does not auto-flash
firmware updates. Drive firmware distribution is vendor-gated,
platform-specific, and not legally redistributable in most cases, so
updating firmware is an explicit manual operation. The UI surfaces the
current firmware version on each drive's detail page for operator
reference.

See [BUILD.md](BUILD.md) for the full design rationale on why automatic
firmware flashing is out of scope.

---

## Development

```bash
git clone https://github.com/JT4862/driveforge.git
cd driveforge
uv venv
source .venv/bin/activate
uv pip install -e '.[dev,linux]'
pytest
driveforge-daemon --dev --fixtures tests/fixtures/
# open http://localhost:8080
```

See [BUILD.md](BUILD.md#development-environment) for the full three-tier
dev environment setup (macOS primary, Debian VM via Lima for
integration testing, R720 for real-hardware validation).
