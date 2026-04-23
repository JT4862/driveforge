"""Fleet helpers — enrollment tokens, agent auth, config munging.

Shared between:
- Operator REST endpoints (`daemon/app.py`) that generate tokens and
  consume agent handshakes.
- Agent-side CLI `driveforge fleet join` that consumes a token from
  the operator and writes the resulting long-lived bearer to
  `fleet.api_token_path`.
- Future (v0.10.1+) WebSocket handshake auth that validates a
  token against `agents.api_token_hash` on every reconnect.

Design notes:

- Tokens are plain random URL-safe strings — 32 bytes from
  `secrets.token_urlsafe(32)` = 43 char base64, which is short enough
  for an operator to eyeball + paste on an agent console.
- Only hashes are stored in the DB. `_hash_token()` uses SHA-256 (no
  salt needed: tokens are already 256 bits of entropy, so a
  per-token salt adds no security). On validation, we hash the
  presented token and compare to the stored hash in constant time.
- Enrollment tokens are one-shot + TTL'd; long-lived agent tokens
  are unbounded by TTL but revocable via `agents.revoked_at`.
- Agent IDs are short hex (8 bytes → 16 chars) so they fit in log
  lines without wrapping. Collision probability at fleet scale (max
  a few dozen agents) is negligible.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from driveforge.db import models as m


# ----------------------------- token primitives


def _new_agent_id() -> str:
    """16-char hex agent ID. Enough entropy for a homelab-scale fleet."""
    return secrets.token_hex(8)


def _new_token_id() -> str:
    """16-char hex enrollment-token ID."""
    return secrets.token_hex(8)


def _new_token_string() -> str:
    """Raw token presented to the operator (via CLI) and later to the
    agent (via the `driveforge fleet join` command). 32-byte urlsafe
    base64 = 43 chars of entropy."""
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    """SHA-256 hex digest. Constant-time comparison is done via
    `hmac.compare_digest` at validation time."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verify_token(presented: str, expected_hash: str) -> bool:
    """Constant-time compare to resist timing-leak side-channels."""
    return hmac.compare_digest(_hash_token(presented), expected_hash)


# ------------------------------ enrollment flow


@dataclass(frozen=True)
class EnrollmentTokenIssue:
    """Return value from `issue_enrollment_token()` — carries the raw
    token string that the operator displays to the user ONCE. The DB
    only persists the hash."""
    token_id: str
    raw_token: str
    expires_at: datetime


def issue_enrollment_token(
    session: Session,
    *,
    ttl_seconds: int = 900,
) -> EnrollmentTokenIssue:
    """Generate a one-shot enrollment token, persist its hash, return
    the raw string to the caller. Caller surfaces the raw string to
    the operator exactly once (via Settings UI or CLI output) — the
    DB has no way to recover it later."""
    token_id = _new_token_id()
    raw = _new_token_string()
    now = datetime.now(UTC)
    row = m.EnrollmentToken(
        id=token_id,
        token_hash=_hash_token(raw),
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(row)
    session.commit()
    # Compose the wire-format token as `<token_id>.<raw>` so the
    # consume path can look up the hash row in O(1) instead of
    # scanning every live token. The token_id is not secret — the
    # raw half is the keyed material.
    composite = f"{token_id}.{raw}"
    return EnrollmentTokenIssue(token_id=token_id, raw_token=composite, expires_at=row.expires_at)


@dataclass(frozen=True)
class EnrollmentResult:
    """Return value from `consume_enrollment_token()`."""
    agent_id: str
    api_token: str  # raw long-lived agent token, shown once to the caller


class EnrollmentError(Exception):
    """Raised when a presented enrollment token is invalid, expired,
    or already consumed."""


def consume_enrollment_token(
    session: Session,
    *,
    composite_token: str,
    display_name: str,
    hostname: str | None,
    version: str | None,
) -> EnrollmentResult:
    """Validate + consume an enrollment token, create the Agent row,
    return the long-lived agent token. One-shot: re-presenting the
    same enrollment token raises EnrollmentError.

    The agent calls this (indirectly, via the operator's enrollment
    HTTP endpoint) during `driveforge fleet join`. The resulting
    `api_token` is what the agent persists to `fleet.api_token_path`
    and presents on every subsequent WebSocket handshake.
    """
    # Parse the composite token: `<token_id>.<raw>`
    if "." not in composite_token:
        raise EnrollmentError("malformed enrollment token")
    token_id, raw = composite_token.split(".", 1)

    row = session.get(m.EnrollmentToken, token_id)
    if row is None:
        raise EnrollmentError("unknown enrollment token")
    if row.consumed_at is not None:
        raise EnrollmentError("enrollment token already consumed")
    now = datetime.now(UTC)
    # Normalize expires_at to aware UTC — SQLite roundtrips drop tzinfo.
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < now:
        raise EnrollmentError("enrollment token expired")
    if not _verify_token(raw, row.token_hash):
        raise EnrollmentError("invalid enrollment token")

    # Mint the long-lived agent identity + token.
    agent_id = _new_agent_id()
    agent_token = _new_token_string()
    agent_composite = f"{agent_id}.{agent_token}"

    session.add(m.Agent(
        id=agent_id,
        display_name=display_name,
        hostname=hostname,
        version=version,
        api_token_hash=_hash_token(agent_token),
        enrolled_at=now,
        last_seen_at=now,
    ))
    row.consumed_at = now
    row.consumed_by_agent_id = agent_id
    session.commit()

    return EnrollmentResult(agent_id=agent_id, api_token=agent_composite)


def authenticate_agent(session: Session, composite_token: str) -> m.Agent | None:
    """Look up an Agent by presented long-lived token. Returns the
    row on match + not-revoked, None otherwise. Used by the v0.10.1+
    WebSocket handshake — pre-wired here so the DB schema + token
    format is stable before the transport lands."""
    if "." not in composite_token:
        return None
    agent_id, raw = composite_token.split(".", 1)
    agent = session.get(m.Agent, agent_id)
    if agent is None:
        return None
    if agent.revoked_at is not None:
        return None
    if not _verify_token(raw, agent.api_token_hash):
        return None
    return agent


# ----------------------------- agent management


def list_agents(session: Session, *, include_revoked: bool = True) -> list[m.Agent]:
    """All agents, newest-enrolled first. Operator's Agents page calls
    this."""
    q = session.query(m.Agent).order_by(m.Agent.enrolled_at.desc())
    if not include_revoked:
        q = q.filter(m.Agent.revoked_at.is_(None))
    return list(q)


def revoke_agent(session: Session, agent_id: str) -> bool:
    """Stamp `revoked_at` on an agent. Idempotent — returns True if a
    row was flipped, False if the agent was already revoked or
    missing. Does NOT delete the row — drive/run history with
    host_id = agent.id stays joinable."""
    agent = session.get(m.Agent, agent_id)
    if agent is None:
        return False
    if agent.revoked_at is not None:
        return False
    agent.revoked_at = datetime.now(UTC)
    session.commit()
    return True


def touch_agent_last_seen(session: Session, agent_id: str) -> None:
    """Called on every WebSocket heartbeat (v0.10.1+). Cheap upsert
    of `last_seen_at`."""
    agent = session.get(m.Agent, agent_id)
    if agent is None:
        return
    agent.last_seen_at = datetime.now(UTC)
    session.commit()


# --------------------- agent-side token persistence


def write_agent_token(path: Path, composite_token: str) -> None:
    """Write the long-lived agent token to disk with mode 600 and,
    when running as root on Linux, chown to the daemon user so the
    unprivileged daemon process can read its own token.

    Discovered in v0.10.5 walkthrough: the CLI writes via `sudo
    driveforge fleet join` (root uid), so without an explicit chown
    the token lands `root:root 0600` and the `driveforge` systemd
    unit (User=driveforge) can't read it. The fleet client's
    `read_agent_token()` silently raises PermissionError inside the
    lifespan task, which asyncio.create_task swallows — leading to
    the symptom "enrollment succeeded, daemon restarted, but
    `fleet status` shows connected=false and no journal log about
    connecting."

    Fix: after writing, if we're root AND the `driveforge` user
    exists, chown to `driveforge:driveforge`. Non-Linux / non-root /
    non-systemd setups (dev, macOS, custom service accounts) fall
    through with the file as-is; callers there are responsible for
    ownership.

    Tests pass a tmp_path override that won't be readable via the
    daemon path anyway — they just verify mode 600 + content, which
    still works after the chown is skipped on non-root test runs.
    """
    import os
    import pwd

    path.parent.mkdir(parents=True, exist_ok=True)
    # Write via a tmp file + rename for atomicity.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(composite_token, encoding="utf-8")
    tmp.chmod(0o600)
    # v0.10.6+ chown when we have the privilege and the target user
    # exists. Keep failures non-fatal so a misconfigured install
    # doesn't crash enrollment; the daemon's next startup logs a
    # clear error and the operator can chown manually.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            daemon_user = pwd.getpwnam("driveforge")
            os.chown(str(tmp), daemon_user.pw_uid, daemon_user.pw_gid)
        except KeyError:
            # `driveforge` user not present — dev box, non-standard
            # install, whatever. Fall through; file stays root-owned.
            pass
        except OSError:
            # Odd filesystem or EPERM despite euid==0 (container?).
            # Log via stderr-ish path isn't worth here; caller gets
            # back a file they at least own + can read.
            pass
    tmp.replace(path)


class AgentTokenUnreadable(Exception):
    """Raised by `read_agent_token` when the file exists but the
    current process can't read it (permission / ownership issue).

    v0.10.6+ distinguishes this from the "file absent" case (returns
    None) so the fleet client can log a clear, actionable error
    instead of silently exiting the lifespan task."""


def read_agent_token(path: Path) -> str | None:
    """Read the long-lived agent token from disk.

    Returns None when the file doesn't exist (agent not yet
    enrolled) or is empty. Raises AgentTokenUnreadable when the
    file exists but can't be read (wrong ownership, selinux label,
    etc.) — caller logs + bails cleanly.
    """
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except PermissionError as exc:
        raise AgentTokenUnreadable(
            f"cannot read {path}: {exc}. The daemon user needs read "
            f"access — try: sudo chown driveforge:driveforge {path}"
        ) from exc
    return content or None
