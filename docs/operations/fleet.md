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
| Start a new batch | Yes — if any selected drives live on an agent |
| Abort a drive | Yes |
| Identify / Stop Identify LED | Yes (toggle state read from the agent's latest snapshot) |
| Regrade | Yes — agent runs the regrade locally against its SMART + source run |

Commands ride the same WebSocket as snapshots. Results arrive via
`CommandResultMsg` frames; failures flash as warning banners on the
operator dashboard ("abort refused on SERIAL: drive in secure_erase
phase"). Successful commands just show the new state on the next
snapshot (≤3 s later).

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
mode. For deployments in less-trusted networks:

- **Audit log**: every enrollment is logged to the operator's
  journal (INFO level) with source IP + hostname + version.
  `journalctl -u driveforge-daemon | grep auto-enrolled` shows
  exactly when and from where.
- **Revoke button** is one click; a rogue agent can be evicted
  from the fleet the moment you spot it on the Agents page.
- **Approval-required mode** (opt-in): Settings toggle gates every
  enrollment behind explicit operator approval. Good for shared
  VLANs.

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

1. **Per-box, via operator web UI**: operator can trigger updates
   on its own.
2. **Per-box, via CLI**: `sudo systemctl start driveforge-update.service`
   on any agent or operator.
3. **Fleet-wide**: in development — future release will add a
   single "Update N agents" button that pushes the update to every
   online agent simultaneously.

For now, SSH to each box and run the update command, or roll a new
ISO and reinstall via "DriveForge Agent" boot entry — the install
preserves the existing agent token.

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
