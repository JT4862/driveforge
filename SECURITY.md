# Security Policy

## Supported versions

DriveForge is single-tracked on the latest tagged release. Older versions
don't receive backported fixes — update to the current release to pick up
security patches.

| Version | Supported |
|---------|-----------|
| Latest release (current `main`) | ✅ |
| Older releases | ❌ |

## Reporting a vulnerability

**Please do NOT open a public GitHub issue for security reports.** Public
disclosure before a fix is available gives attackers a head start on the
people actually running the software.

### Preferred channel

Open a [private security advisory](https://github.com/JT4862/driveforge/security/advisories/new)
on GitHub. This creates a confidential conversation with the maintainer
and gives both sides a structured place to coordinate fixes, CVE
assignment (if warranted), and disclosure timing.

### Fallback

If for any reason the GitHub advisory flow isn't working for you, email
the maintainer at the address listed in `pyproject.toml`'s `authors`
field. Put **`[SECURITY]`** in the subject line so it routes to the top
of the inbox.

## What to include

- Affected version(s) / commit
- A clear description of the issue and why it's a security concern
- Reproduction steps (ideally minimal)
- Impact — what can an attacker do with this?
- Any mitigations you've identified

## Response expectations

This is a solo-maintained homelab project, so response times are
best-effort — usually within a week, occasionally longer. The maintainer
will acknowledge receipt, work on a fix, and coordinate disclosure with
the reporter.

## Scope

### In scope

- Remote code execution via the web UI or API
- Authentication bypass (the dashboard currently has **no authentication**
  — that's a known design choice for a LAN-only appliance, but a way to
  bypass it from outside the local network is still worth reporting)
- Privilege escalation beyond what the daemon's `CAP_SYS_RAWIO` +
  `CAP_SYS_ADMIN` capabilities already grant
- SQL injection, path traversal, SSRF in `core/webhook.py`
- Credential leakage in logs, cert pages, or the DB
- Drive data leakage between different users' runs (the `/reports/*`
  public pages by design show grade + rationale + SMART — but never
  should leak serial numbers across tenants, filenames from the host OS,
  etc.)
- Supply-chain issues in the offline bundle (ISO install path — the .debs
  and wheels it ships)

### Out of scope

- **Drive destruction by design.** DriveForge secure-erases every drive in
  a batch. That's the whole point of the app. Reports of "it erased my
  drive" are not security issues — they're intended behavior.
- **Dashboard has no login.** On the LAN side, any host that can reach
  port 8080 can view + control the app. That's a deliberate design
  constraint for a homelab appliance, documented in
  [BUILD.md](BUILD.md#security-model). Reports that call this out as
  "insecure" without a specific remote-exploit pathway will be closed
  with a pointer to the design doc.
- **Physical access bypasses everything.** Drive-destructive hardware
  tools necessarily trust physical access to the rig. Attacks requiring
  console/root on the server itself are out of scope.
- **Vulnerabilities in Debian, Python, or pip packages.** Report those
  upstream. DriveForge tracks upstream security updates via
  `apt-get update && apt-get upgrade` — standard OS hygiene.

## Public disclosure timeline

- **Day 0**: Report received, acknowledged.
- **Day 1-14**: Triage + fix development.
- **Day 14-30**: Fix landed and released; advisory published.
- **Longer if necessary** for complex issues, coordinated with the
  reporter.

The maintainer commits to public credit (unless you ask for anonymity)
in the advisory and the release notes.
