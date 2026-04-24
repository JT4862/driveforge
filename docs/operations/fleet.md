---
title: Fleet mode
---

# Fleet mode (v0.10+)

DriveForge's default shape is single-box: one server, one dashboard,
drives plugged into local bays. **Fleet mode** lets one DriveForge
install — the *operator* — aggregate drives from other DriveForge
boxes — *agents* — onto a single dashboard. You manage the whole
fleet from the operator's UI; agents are headless workers.

Useful when you have extra old servers lying around that could
burn-in drives but don't justify their own dashboard. The operator
gets one pane of glass: every drive in the fleet, regardless of
which box it's plugged into, on one screen.

## Roles

| Role | What it does |
|---|---|
| **standalone** (default) | Single box, local dashboard, local drives. Same as pre-v0.10 behavior. |
| **operator** | Serves the web UI. Aggregates drives from enrolled agents. **Also runs its own local pipeline** — the operator is a standalone + fleet aggregation. Single-box users never see fleet concepts. |
| **agent** | Headless worker. Registers with an operator, reports local drives + pipeline state, executes commands the operator sends. No web UI — just API + the fleet WebSocket. |
| **candidate** (v0.11+) | Freshly-installed box that hasn't joined any fleet yet. Advertises itself via mDNS so an operator on the LAN can adopt it with one click. Becomes an agent on adoption. |

A single DriveForge install can be flipped between standalone and
operator at any time from Settings → Fleet. Agent mode is entered
by running the setup wizard's "Agent" option, by booting the
"DriveForge Agent" ISO entry, or by running
`sudo driveforge fleet join` on the console.

---

## Building a fleet

### 1. Install the operator

Install DriveForge normally on the box that will be the brain —
typically the one with the printer attached and/or the most
resilient hardware. In the setup wizard Step 1, pick **Operator**
(or leave it as Standalone and flip later from Settings → Fleet).
Restart the daemon when the Settings page prompts.

Operator mode gives you:

- Dashboard that aggregates drives from every agent
- Settings → Agents page with enrollment + discovery
- Cert printer that serves the entire fleet
- Fleet-wide configuration (auto-enroll toggle applies to every agent)

### 2. Install an agent (recommended: ISO "DriveForge Agent" entry)

Boot your next server from the DriveForge ISO USB stick. At the
boot menu, pick:

```
DriveForge Agent (auto-join fleet on this network)
```

instead of the default "DriveForge" entry. The installer runs
unattended — no wizard, no prompts. When it finishes, the box boots
into **candidate** mode:

- No web UI
- Advertises itself on the LAN via mDNS
  (`_driveforge-candidate._tcp.local`) with its hostname, version,
  and a stable install-id

### 3. Enroll the candidate (one click)

On the operator's dashboard, open Settings → Agents. Within ~15
seconds of the candidate coming online, a new section appears:

> **Discovered on network** (1 candidate advertising on your LAN)
>
> | Hostname | IP | Version | Install ID | |
> |---|---|---|---|---|
> | driveforge-a1b2c3 | 10.0.0.5 | 0.11.0 | a1b2c3d4 | [Enroll] [Ignore] |

Click **Enroll**. The operator mints a long-lived agent credential
and POSTs it directly to the candidate over the LAN; no tokens or
URLs to copy. The candidate writes the credential, flips to agent
mode, and restarts its daemon. Within ~10 seconds the agent appears
on the operator's dashboard as an online fleet member; any drives
plugged into its bays show up with a colored **host badge**
identifying which box they're on.

**Ignore** hides the candidate from the panel (e.g. a neighbor's
DriveForge box accidentally visible on a shared VLAN).

### 4. Repeat for each agent

The ISO works for any number of agents — stock a shelf of USB
sticks and use the same one for every new box. mDNS handles
discovery on a flat LAN; the install-id ensures no two
simultaneously-advertising candidates collide on the operator's
panel.

---

## The manual path (automation / VLAN-isolated boxes)

The ISO + mDNS flow assumes a flat LAN. If multicast is blocked or
you're scripting provisioning from Ansible / a PXE flow, the
older v0.10.0 manual path still works:

1. On the operator: Settings → Agents → **Generate enrollment token**.
2. Copy the displayed one-liner (a time-limited, single-use token).
3. On the agent console:
   ```bash
   sudo driveforge fleet join http://operator.local:8080 <token>
   ```
4. The CLI enrolls the box, writes the credential to
   `/etc/driveforge/agent.token` (mode 0600, owned `driveforge:driveforge`),
   flips the role, restarts the daemon.

Enrollment tokens are one-shot, 15-minute TTL, SHA-256 hashed at
rest on the operator. Lose one? Generate another.

---

## Converting an existing standalone into an agent

Already have DriveForge running as standalone and want to move it
into a fleet? Two options:

- **Setup wizard** — on the agent, Settings → "Replay setup wizard"
  then pick "Agent (headless)" in Step 1. Daemon restarts in
  candidate mode; enroll from the operator.
- **CLI** — same `driveforge fleet join` command as the manual
  path above.

Either path preserves the drive history DB. Agents keep running
any in-flight local pipelines during the role-flip — you won't
interrupt a drive mid-badblocks.

---

## What the operator sees

### Dashboard

Drives from every agent render inline with the operator's local
drives. Each card carries a small **host badge** in the top-right
showing the source agent's display name (e.g. `r720-bench`).
Operator-local drives have no badge.

A **host filter pill row** at the top of the grid lets you scope
the view to a single box:

```
VIEW: [All hosts 24] [this operator 6] [r720-bench 12] [nx3200-jbod 6]
```

Click a pill to show only that host's drives.

### Settings → Agents

Three sections:

1. **Discovered on network** — candidates advertising via mDNS
   that haven't been enrolled yet. Enroll / Ignore per row.
2. **Enrolled agents** — table of every agent that's ever joined
   this fleet, with hostname, version, enrolled-at, last-seen, live
   connection status (connected / online / offline), plus Rotate
   and Revoke buttons.
3. **Manual enrollment** — "Generate enrollment token" for the
   CLI path.

Plus a **Recent connection refusals** log (capped at 32 entries)
showing rejected handshakes by reason + source IP. Useful for
diagnosing "why isn't my agent showing up" issues without tailing
journals.

---

## Fleet-wide auto-enroll

The **Auto: Off / Quick / Full** pill on the operator dashboard is
fleet-wide. Click Quick → every agent picks up the new value via
the fleet socket within seconds, and the NEXT drive inserted into
any fleet member auto-starts a pipeline in quick mode. Agents that
are offline when you click receive the update on their next
reconnect (via the `hello_ack` handshake).

This is v0.10.9+ behavior — earlier versions had per-agent settings
that drifted.

---

## Agent UX: the box is headless

Agents serve no web GUI. Hitting `http://<agent>.local:8080/` in a
browser returns a plaintext page:

```
DriveForge agent — managed by operator at http://driveforge-op.local:8080.

On this box's console:
  driveforge fleet status  — diagnostics + live connection counters
  driveforge fleet leave   — detach + revert to standalone
```

All drive management — starting pipelines, aborting, regrading,
identifying LEDs — happens from the operator's dashboard. POSTs to
`/batches/new`, `/drives/<s>/abort`, etc. on the agent return 404;
the agent's API-only surface is `/api/*` + `/fleet/ws`.

### Why no agent UI?

- **Single source of truth.** The operator is the canonical
  dashboard. A full UI on the agent invites split-brain (someone
  clicks Abort on the agent while the operator thinks the drive
  is idle).
- **Attack surface.** No Jinja rendering, no Settings save
  endpoints — the per-agent surface is tiny.
- **Label QR correctness.** Cert labels printed from the
  operator's printer carry QR codes that point to the operator's
  dashboard. Since agent DB history is pruned after forwarding,
  a QR pointing to an agent would go stale; routing through the
  operator keeps the QR pointing at permanent records.

---

## The pipeline still runs on the agent

Agents run the full local pipeline — SMART, erase, badblocks, long
self-test, grading. When a run completes, the agent forwards the
cert data upstream via a `RunCompletedMsg` over the fleet
WebSocket. The operator upserts the drive + run rows into its own
DB (with `host_id` set), auto-prints the cert on its local
printer, and acks the agent.

**Local DB on the agent** is a write-ahead log:

- Every phase transition commits to the agent's local SQLite
- Pipeline keeps running during an operator outage (agent doesn't
  block on the WebSocket)
- On reconnect, the agent replays any unacked completions — no
  certs lost even across a long operator downtime
- Once the operator acks, the WAL flag is cleared and pruning
  removes rows older than 24 hours (except the most-recent-per-
  drive, kept for regrade support)

The operator is the canonical record store; the agent's DB is a
transient cache.

---

## Remote commands

The operator's dashboard actions work transparently against remote
drives:

| Action | Forwards to agent? |
|---|---|
| Start a new batch | Yes — agent drives appear in the New Batch form alongside local drives (v0.11.7+); pick any combination across the fleet |
| Abort a drive | Yes |
| Identify / Stop Identify LED | Yes (toggle state read from the agent's latest snapshot) |
| Regrade | Yes — agent runs the regrade locally against its SMART + source run |
| Mark as unrecoverable | Yes (v0.11.10+) — operator clicks the button on a fleet drive's detail page; F-grade stamps via the operator's DB ingestion path; physical UNRECOVERABLE label prints on the operator's QL printer |

Commands ride the same WebSocket as snapshots. Results arrive via
`CommandResultMsg` frames; failures flash as warning banners on the
operator dashboard ("abort refused on SERIAL: drive in secure_erase
phase"). Successful commands just show the new state on the next
snapshot (≤3 s later).

### Batches that span the fleet (v0.11.9+)

When the operator's New Batch form fans out across one local drive
and one agent drive, both rows land under the same `batch_id` on
the operator's batch detail page. Pre-v0.11.9 the agent's TestRun
was orphaned (`batch_id=None`) and the batch view only listed local
rows. Now `StartPipelineCmd` carries the operator-minted batch_id
down to each agent, the agent's TestRun row joins back to the
operator's Batch row on completion ingestion, and the batch view
shows the full roster with host badges. Click into any row to see
the per-drive run details.

### Remediation panels for fleet drives (v0.11.10+)

Both the v0.6.9 frozen-SSD remediation panel and the v0.9.0
password-locked panel work for drives that live on agents. Pre-
v0.11.10 the panel only rendered when the orchestrator-side state
(in-memory `DaemonState.frozen_remediation`) had an entry — but the
agent's orchestrator registers on the AGENT's state, not the
operator's, so fleet drives silently lost their remediation panel
on the operator. Same loss happened on standalone after a daemon
restart (in-app update wiped the dict). The fix derives panel
state from the latest TestRun's error pattern when no in-memory
entry exists — both classes resolve cleanly now. The Mark-as-
Unrecoverable button on either panel triggers the physical label
print regardless of whether the drive lives locally or on an agent.

---

## Security model

DriveForge fleet mode assumes a **trusted LAN**. The operator ↔
agent WebSocket uses bearer-token authentication but no mTLS; mDNS
discovery is unauthenticated by design (see
[Security — threat model](#security--threat-model) below). This is
fine for homelab and small-refurb-shop environments where the
network itself is the trust boundary.

### What's secured

- **Bearer tokens** are 256-bit random, SHA-256 hashed at rest.
  Constant-time compare on every handshake.
- **Revocation** kicks the active WebSocket immediately (not on
  next reconnect).
- **Rotation** is one-click from the operator's Agents page:
  revokes the current credential, mints a new enrollment token,
  you paste it on the agent's console.
- **Connection refusals** are logged (reason, source IP,
  agent_id presented) for audit.

### Security — threat model

The risk worth naming: an attacker with LAN access can install
DriveForge on their own box, let it be adopted, and submit a
fabricated `RunCompletedMsg` creating a "Grade A" cert for a
serial they don't physically own. The QR on that cert would point
to your operator's dashboard, legitimizing the fake record.

If your LAN is compromised, you have bigger problems than fleet
mode. What we DO have today:

- **Operator-initiated enrollment**. Candidates advertise
  themselves but don't auto-join anything — the operator has to
  click Enroll on the Settings → Agents → Discovered panel. That
  click IS the approval gate; there's no unauthenticated auto-adoption.
- **Audit logging**. Adoption logs at INFO with the candidate's
  install_id, hostname, and IP:
  `journalctl -u driveforge-daemon | grep "fleet: adopting"`.
- **Revoke** is one click; a rogue agent can be evicted from the
  fleet the moment you spot it on the Agents page.
- **Recent connection refusals** panel on Settings → Agents shows
  rejected handshakes (wrong token, revoked, protocol skew, etc.)
  with source IP for diagnostic + audit use.

mTLS on the fleet socket is on the post-v1.0 roadmap for operators
who want stronger network-layer identity.

---

## Removing an agent

- **Temporary**: click **Revoke** on Settings → Agents. Active
  WebSocket closes immediately; agent's reconnect attempts are
  refused with "token revoked." Historical drive/run rows stay
  attributed to the agent for auditing.
- **Back to standalone**: on the agent's console, run
  `sudo driveforge fleet leave`. Clears the role + operator URL +
  removes the agent token. Daemon restarts in standalone mode.
  Still-present local drives reappear under its own hostname if
  you bring the web UI up again.

---

## Running multiple fleets on one LAN

Each operator install has a unique `fleet_id` (auto-generated at
first boot, persisted in YAML). mDNS TXT records include it so
agents running a future feature can pick the right fleet to join
— for v0.11+ the candidate just advertises and the operator picks
it up, so `fleet_id` mostly matters for distinguishing
discovered-on-network rows when you have two operators visible on
the same LAN.

Two operators will each discover candidates independently; whichever
operator clicks Enroll first wins. If you run two fleets on purpose
(e.g. production + test), they don't interfere — each operator
only adopts candidates you explicitly click Enroll for, and each
operator's Agents page is its own world.

---

## Upgrading the fleet

`driveforge-update.service` (polkit-authorized) works on every
role, including agents. Three ways to update:

1. **Fleet-wide, one click** (v0.11.4+, verified-delivery in v0.11.6+):
   on the operator's Settings → Updates panel, click **Install update
   now**. The operator broadcasts an `UpdateCmd` to every connected
   agent, waits up to 5 seconds per agent for an ACK
   (`CommandResultMsg`), then triggers its own update. Each agent
   updates independently via its local
   `driveforge-update.service`. The operator's redirect URL surfaces
   `fleet_pushed=N&fleet_acked=M&fleet_failed=name1,name2` so the
   Settings page can render a per-agent failure banner with manual
   recovery commands. Failed/timed-out agents do NOT block the
   operator's own update — they just appear in the failed list.
2. **Per-box, via operator web UI**: visiting an individual agent
   isn't possible (agents are headless), but the fleet-wide button
   above is the supported path.
3. **Per-box, via CLI**: `sudo systemctl start driveforge-update.service`
   on any agent or operator. Useful when the in-app update path is
   itself broken (rare — the v0.11.8 release fixed a class of dead-
   button bugs caused by `window.confirm()` blocking, and v0.11.10
   removed the same pattern from every other form across the app).

The fleet-wide button is the documented happy path. SSH-as-fallback
exists but should be needed only for bootstrap (installing the first
update that itself fixes the in-app update path).

---

## Troubleshooting

### "My agent doesn't appear on the operator's Discovered panel"

Check in order:

1. **Both boxes on the same LAN / subnet?** mDNS doesn't cross VLANs
   by default. Use the manual enrollment path instead.
2. **Can the operator see the candidate at all?** On the operator:
   ```bash
   avahi-browse -r -t _driveforge-candidate._tcp
   ```
   If this is empty, the candidate isn't advertising; check that
   `avahi-daemon` is running on the candidate.
3. **Has the candidate finished booting?** Fresh installs take
   ~15 seconds after boot before advertising kicks in.
4. **Is there a hostname collision?** See
   [Hostname rename](hostname-rename.md) — every box needs a
   unique hostname for mDNS.

### "Enroll clicked, candidate never became an agent"

Check the operator's Settings → Agents → Recent connection refusals.
If the candidate isn't reachable at the IP shown in the Discovered
panel, the operator will have logged a refusal with the reason.
Likely causes:

- Candidate's daemon crashed between advertise and adopt
- Firewall on the candidate blocks inbound from the operator
- DHCP lease expired and the IP changed between discover + enroll

### "Operator's dashboard shows ghost drives that aren't there"

Likely a stale cached entry from before v0.10.6 when the snapshot
builder could leak DB history. Upgrade both boxes to v0.10.7+ and
restart the daemon on the agent — the next snapshot will be
presence-accurate.

### "Agent says 'operator unreachable' forever"

Check the operator's hostname:

```bash
# on the agent:
cat /etc/driveforge/driveforge.yaml | grep operator_url
curl -v http://<operator-host>:8080/api/health
```

If the operator renamed its host, the agent's stored URL is stale.
Either rename via mDNS (`.local` names follow the rename) or
re-enroll the agent with the new URL via
`sudo driveforge fleet leave && sudo driveforge fleet join <new-url> <token>`.

### "Want to see live fleet counters"

On the agent:

```bash
driveforge fleet status
```

Shows role, operator URL, connected bool, snapshots/heartbeats/completions
sent, reconnect attempts, last error. Probes the local daemon at
`/api/fleet/local-status` under the hood.

On the operator: Settings → Agents shows per-agent live status
(connected / online / offline) alongside the DB-level `last_seen_at`.
