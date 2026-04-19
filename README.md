# DriveForge

**In-house enterprise drive refurbishment pipeline.**

DriveForge turns a dedicated Debian server (typically a Dell PowerEdge R720)
into an automated drive testing, grading, and certification rig. Pulled
enterprise drives go in; SMART-validated, secure-erased, burned-in, graded,
cert-labeled drives come out — ready for the homelab.

- **Status**: pre-alpha, in active development
- **License**: MIT
- **Requires**: Debian 12 + Python 3.11+ on the server; any x86_64 hardware
  with drive bays (HBA in IT mode recommended)

See [BUILD.md](BUILD.md) for the full design document and [CLAUDE.md](CLAUDE.md)
for dev session context.

## What it does

1. Auto-discovers drives on plug-in (udev hotplug)
2. Per-drive pipeline in parallel (up to 8 drives / R720 bay):
   - Pre-test SMART baseline
   - SMART short self-test
   - Firmware check (NVMe auto-update opt-in)
   - Secure erase (SATA `hdparm --security-erase` / SAS `sg_format` / NVMe `nvme format`)
   - `badblocks` destructive write/read
   - SMART long self-test
   - Post-test SMART diff → grade (A / B / C / fail)
   - Thermal printer cert label (Brother QL family)
   - Optional outbound webhook (n8n / Zapier / custom)
3. Serves a local web UI at `http://driveforge.local:8080` showing live
   bay state, per-drive SMART history, and a public-facing QR-coded
   report page.

## Install

```bash
curl -sSL https://raw.githubusercontent.com/JT4862/driveforge/main/scripts/install.sh | sudo bash
```

(Pre-alpha — install script lands with the first release.)

## Development

```bash
git clone https://github.com/JT4862/driveforge.git
cd driveforge
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
pytest
driveforge-daemon --dev --fixtures tests/fixtures/
# open http://localhost:8080
```

See [BUILD.md](BUILD.md) → Development Environment for the full three-tier
dev environment setup (macOS primary, Debian VM via Lima for integration,
R720 for real-hardware validation).

## Firmware update limitations

DriveForge can auto-apply firmware updates for NVMe drives and many SATA /
SAS drives when a signed known-good blob is available, but the following
categories are explicitly **not** supported:

- **OEM-branded drives** (Dell / HP / NetApp) — custom firmware strings
  that don't accept retail blobs
- **Windows-only vendor tools** (Samsung Magician, some HGST WinDFT) —
  reported as "manual flash required"
- **Drives requiring physical power cycle after flash** — DriveForge can't
  power-cycle individual R720 bays; user reseats after commit
- **Vendor-support-gated firmware** — Dell / HP enterprise drives whose
  firmware is behind a support login

See [BUILD.md](BUILD.md) → Firmware Updates for the full safety model.
