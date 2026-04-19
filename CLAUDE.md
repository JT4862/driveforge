# DriveForge — Claude session notes

## Read first
- **[BUILD.md](BUILD.md)** — the canonical project plan. Architecture, phases,
  decisions, UI design, telemetry scope, OSS distribution policy. Every
  substantive design choice lives there.
- User memory at `~/.claude/projects/-Users-jt/memory/` — JT's profile,
  homelab context, red lines.

## Project summary
DriveForge is a Debian + Python app that turns a Dell PowerEdge R720 into
an in-house drive refurbishment pipeline: SMART → secure-erase → burn-in →
long self-test → grade → print cert label. Commercial-refurbisher workflow
at homelab scale.

**Current status: pre-Phase-1.** BUILD.md is complete; no code exists yet.
Do not assume prior sessions wrote anything — check `git log` first.

## Workflow rules
- Branch: `main` only
- Commits: imperative subject, short body, Co-Authored-By trailer (see
  existing commits for the pattern)
- Tests: `pytest` with recorded SMART / nvme-cli / ipmitool fixtures —
  drive hardware is NOT required for dev or CI
- Python: 3.11+, `uv` or venv

## Appliance philosophy (non-negotiable, do not re-litigate)
- **No YAML hand-editing by users.** All configuration lives in the
  Settings UI. YAML on disk is daemon-written.
- **Self-contained by default.** No external service dependencies ship
  enabled. One opt-in outbound webhook is the only integration point;
  Twenty CRM, n8n, etc. are routed by the user downstream of the webhook.
- **Debian owns the OS** (network config via netplan, packages, systemd).
  DriveForge owns the app. Don't let DriveForge manage static IPs.
- **Brother QL printer family** driven by `brother_ql` (raster protocol).
  **Not** `python-escpos` — that's ESC/POS, wrong protocol. Past session
  hit this trap.

## Before writing code
Phase 1 is next: `pyproject.toml` + package skeleton + MIT LICENSE +
daemon skeleton running one drive through smartctl + badblocks + erase
against recorded fixtures. See BUILD.md → MVP Milestones.
