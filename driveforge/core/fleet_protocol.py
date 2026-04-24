"""Fleet WebSocket wire protocol (v0.10.1+).

All messages are JSON, with a `msg` discriminator field. Pydantic
validates every inbound message; unknown `msg` types are dropped
with a warning (forward-compat: agents running a newer protocol can
safely send messages the operator doesn't understand yet).

Protocol version is separate from the daemon's semver so patch
releases don't break the fleet. `PROTOCOL_VERSION` bumps only when
the wire format changes in a non-backward-compatible way.

### Message flow

Agent → operator:
- `hello` (first frame after connect): agent announces itself
- `drive_snapshot`: full list of currently-attached drives + their
  live state. Sent on connect and every N seconds thereafter.
- `heartbeat`: keep-alive ping with agent-side timestamp

Operator → agent:
- `hello_ack`: operator acknowledges hello, returns version info
- `ack`: generic acknowledgment with the message's correlation id

### Design choices

- **Snapshot-based, not delta-based (for v0.10.1).** Every N seconds
  the agent sends a full drive list rather than per-drive deltas.
  Simpler to reason about on the operator side (no reconciliation
  needed), and the snapshot size is small (a handful of drives × a
  few hundred bytes each). v0.10.2+ may introduce deltas if the
  snapshot becomes too chatty.

- **No explicit ack required for snapshots.** The agent keeps its
  local WAL until the operator sends an `ack` for specific runs
  (v0.10.3). Live state is fire-and-forget — if a snapshot is lost,
  the next one (≤3s later) supersedes it.

- **Bearer-token auth, not mutual TLS.** v0.10.1 ships with bearer
  tokens in the WebSocket handshake `Authorization` header. mTLS is
  on the v0.10.4 list; bearer tokens are fine for LAN-scoped
  homelab use until then.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


# Bumped only when the wire format breaks backward compatibility.
# Operators refuse connections from agents whose major component
# differs from their own.
PROTOCOL_VERSION = "1.0"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ----------------------------------------------- agent → operator


class HelloMsg(BaseModel):
    msg: Literal["hello"] = "hello"
    agent_id: str
    display_name: str
    hostname: str | None = None
    agent_version: str
    protocol_version: str = PROTOCOL_VERSION


class DriveState(BaseModel):
    """One drive's live state snapshot. Mirrors the per-serial fields
    from `DaemonState` on the agent side. All fields except serial +
    model are optional — if a drive isn't currently active, the
    phase/percent/etc. are None."""
    serial: str
    model: str
    capacity_bytes: int
    transport: str  # "sata" | "sas" | "nvme"
    manufacturer: str | None = None
    rotational: bool | None = None
    firmware_version: str | None = None
    # Kernel device basename ("sda", "nvme0n1"). Optional + local to
    # the agent — never used by the operator except for display /
    # debugging, since kernel letters drift across reboots.
    device_basename: str | None = None
    # Live-run state (None when drive is idle)
    phase: str | None = None
    percent: float | None = None
    sublabel: str | None = None
    io_rate: dict[str, float] | None = None  # {"read_mbps", "write_mbps"}
    drive_temp_c: int | None = None
    # When the current phase started, for the dashboard's pulse
    # animation. Operator renders relative to its own clock.
    phase_change_ts_epoch: float | None = None
    # v0.10.2+ — is the agent currently running an LED identify
    # blinker on this drive? Exposed so the operator's toggle button
    # on remote drives reflects the true agent state (rather than the
    # operator guessing from a self-maintained shadow map).
    identifying: bool = False


class DriveSnapshotMsg(BaseModel):
    msg: Literal["drive_snapshot"] = "drive_snapshot"
    # Full list of drives this agent currently sees. Empty list is
    # valid (agent has no drives attached); the operator uses that
    # to clear any stale rows.
    drives: list[DriveState]
    # Monotonic clock sequence number so the operator can drop an
    # out-of-order snapshot if one arrives late on a flaky link.
    seq: int
    sent_at: datetime = Field(default_factory=_utcnow)


class HeartbeatMsg(BaseModel):
    msg: Literal["heartbeat"] = "heartbeat"
    sent_at: datetime = Field(default_factory=_utcnow)


# ----------------------------------------------- operator → agent


class HelloAckMsg(BaseModel):
    msg: Literal["hello_ack"] = "hello_ack"
    operator_version: str
    protocol_version: str = PROTOCOL_VERSION
    # If the operator refuses the connection (e.g. protocol skew),
    # this carries the reason and the agent should not retry without
    # operator intervention. Present only on a refusal path.
    refused_reason: str | None = None
    # v0.10.9+ fleet-wide config snapshot sent on handshake.
    # auto_enroll_mode: "off" | "quick" | "full" — the operator's
    # current dashboard toggle value. Agents mirror this instead of
    # consulting their own local config so a single "Auto: Quick"
    # click applies to every agent immediately. Optional (default
    # None) for forward-compat: operators running pre-v0.10.9
    # won't send it, and agents running v0.10.9+ treat absence as
    # "off" on the agent side to preserve the fail-closed semantics.
    auto_enroll_mode: str | None = None


class ConfigUpdateMsg(BaseModel):
    """Operator pushes a setting change to all connected agents.

    v0.10.9 uses this exclusively for `auto_enroll_mode` — the only
    operator-scoped setting that affects agent behavior. Future
    settings (retention windows, grading thresholds, etc.) may pile
    onto this message or get their own types; we start narrow to
    avoid over-committing the schema.
    """
    msg: Literal["config_update"] = "config_update"
    auto_enroll_mode: str | None = None


class AckMsg(BaseModel):
    msg: Literal["ack"] = "ack"
    ack_for: str  # name of the message being acked (for logs / debugging)


# v0.10.2+ operator-issued commands. Each carries a `cmd_id` the
# agent echoes in its CommandResultMsg so the operator can correlate
# request + reply. Delivery is fire-and-forget from the operator's
# POST handler's perspective — the operator doesn't block on the
# reply; the drive's next snapshot (≤3s later) shows the result.
# CommandResultMsg is useful for logging + surfacing explicit
# failures that wouldn't otherwise reflect in the drive state
# (e.g. "abort refused: drive in secure_erase phase").


class StartPipelineCmd(BaseModel):
    msg: Literal["start_pipeline"] = "start_pipeline"
    cmd_id: str
    serial: str
    quick_mode: bool = False
    source: str | None = None


class AbortCmd(BaseModel):
    msg: Literal["abort"] = "abort"
    cmd_id: str
    serial: str


class IdentifyCmd(BaseModel):
    """Toggle the LED identify blinker on a drive.

    `on=True` → start the rapid-strobe (or kick an already-running
    blinker's 5-minute deadline, per the current local behavior).
    `on=False` → stop any running identify blinker.
    """
    msg: Literal["identify"] = "identify"
    cmd_id: str
    serial: str
    on: bool


class RegradeCmd(BaseModel):
    """Re-apply current grading thresholds against a drive's prior
    completed A/B/C run. Non-destructive. v0.10.2+ wire format; the
    agent-side dispatcher executes the regrade handler's body
    locally."""
    msg: Literal["regrade"] = "regrade"
    cmd_id: str
    serial: str


# ----------------------------------------------- agent → operator (replies)


class CommandResultMsg(BaseModel):
    """Reply to any operator command. `success=False` → `detail`
    explains the refusal; operator surfaces it in the dashboard
    flash area on the next request."""
    msg: Literal["command_result"] = "command_result"
    cmd_id: str
    command: str  # "start_pipeline" | "abort" | "identify" | "regrade"
    success: bool
    detail: str | None = None


# v0.10.3+ pipeline completion forwarding.
#
# When an agent's pipeline finishes (pass OR fail tier — anything
# that would normally hit the local auto-print path), the agent
# forwards a `RunCompletedMsg` to the operator. Operator upserts
# Drive + TestRun rows into its own DB (host_id = agent_id) and
# fires its own auto-print against the fleet's printer.
#
# A simple WAL pattern guarantees exactly-once delivery across
# network blips: the agent's DB column `test_runs.pending_fleet_forward`
# is flipped to True on completion; a forward loop scans for pending
# rows, sends them, and flips to False only after receiving
# `RunCompletedAckMsg`. Operator-side receipt is idempotent (upsert
# keyed on `completion_id` so a replay after an ack-in-flight drop
# does nothing new).


class CompletedRunData(BaseModel):
    """Serialized TestRun contents — everything we need to recreate
    the row on the operator side AND synthesize the cert label.
    Intentionally flat; not a reference to the agent's DB row."""
    run_id: int  # agent-side TestRun.id, for idempotency correlation
    drive_serial: str
    batch_id: str | None = None
    bay: int | None = None
    phase: str
    started_at: datetime
    completed_at: datetime
    grade: str | None
    triage_result: str | None = None
    power_on_hours_at_test: int | None = None
    reallocated_sectors: int | None = None
    current_pending_sector: int | None = None
    offline_uncorrectable: int | None = None
    pre_reallocated_sectors: int | None = None
    pre_current_pending_sector: int | None = None
    smart_status_passed: bool | None = None
    rules: list[dict] | None = None
    report_url: str | None = None
    label_printed: bool = False
    quick_mode: bool = False
    throughput_mean_mbps: float | None = None
    throughput_p5_mbps: float | None = None
    throughput_p95_mbps: float | None = None
    throughput_pass_means: list[float] | None = None
    error_message: str | None = None
    log_tail: str | None = None
    interrupted_at_phase: str | None = None
    sanitization_method: str | None = None
    # v0.8.0 fields
    lifetime_host_reads_bytes: int | None = None
    lifetime_host_writes_bytes: int | None = None
    wear_pct_used: int | None = None
    available_spare_pct: int | None = None
    end_to_end_error_count: int | None = None
    command_timeout_count: int | None = None
    reallocation_event_count: int | None = None
    nvme_critical_warning: int | None = None
    nvme_media_errors: int | None = None
    self_test_has_past_failure: bool | None = None
    drive_class: str | None = None


class CompletedDriveData(BaseModel):
    """Drive identity, upserted on the operator side. Mirrors the
    `drives` row columns. Capacity + transport + model are the
    critical ones for label rendering."""
    serial: str
    model: str
    manufacturer: str | None = None
    capacity_bytes: int
    transport: str
    rotational: bool | None = None
    firmware_version: str | None = None


class RunCompletedMsg(BaseModel):
    msg: Literal["run_completed"] = "run_completed"
    # Stable idempotency key. Agent mints this once per completion;
    # replays after a reconnect carry the same id so the operator
    # can deduplicate via "has this completion_id already been
    # committed?" check.
    completion_id: str
    drive: CompletedDriveData
    run: CompletedRunData


class RunCompletedAckMsg(BaseModel):
    msg: Literal["run_completed_ack"] = "run_completed_ack"
    completion_id: str
    # If operator couldn't persist (e.g. DB error), this carries
    # the reason and the agent keeps the WAL entry for a later
    # retry instead of pruning it.
    success: bool = True
    detail: str | None = None


# ---------------------------------- helpers


# Union type for inbound (agent → operator) decoding. Used by the
# server handler to dispatch on `msg`.
AgentToOperatorMsg = (
    HelloMsg | DriveSnapshotMsg | HeartbeatMsg | CommandResultMsg
    | RunCompletedMsg
)

# Union for the reverse direction.
OperatorToAgentMsg = (
    HelloAckMsg | AckMsg
    | StartPipelineCmd | AbortCmd | IdentifyCmd | RegradeCmd
    | RunCompletedAckMsg | ConfigUpdateMsg
)


def is_protocol_compatible(their_version: str, our_version: str = PROTOCOL_VERSION) -> bool:
    """Major-version match is required; minor skew is fine.

    v0.10.1 only speaks "1.0", but future releases may add fields.
    Minor bumps (1.1, 1.2) stay compatible — pydantic silently
    ignores unknown fields by default via model_config. Major bumps
    (2.0) are breaking and get refused at handshake.
    """
    try:
        their_major = their_version.split(".", 1)[0]
        our_major = our_version.split(".", 1)[0]
    except (AttributeError, IndexError):
        return False
    return their_major == our_major
