"""Release-update checker.

Hits the GitHub Releases API on demand and reports whether a newer
DriveForge release is available. Manual-only — never auto-updates,
never auto-checks. The operator clicks a button in Settings, the
daemon makes one HTTPS request, the result is cached for an hour to
avoid hammering the API on repeat page loads.

No telemetry is sent — the request body is empty and the User-Agent
identifies DriveForge so GitHub's logs see what tool is asking, but
the request reveals only the operator's IP (which is the same IP that
appears in any other outbound HTTPS request from the box).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from driveforge.version import __version__ as CURRENT_VERSION

logger = logging.getLogger(__name__)

GITHUB_RELEASES_URL = "https://api.github.com/repos/JT4862/driveforge/releases/latest"
CACHE_TTL_SEC = 3600  # one hour
HTTP_TIMEOUT_SEC = 10


@dataclass
class UpdateInfo:
    """Snapshot of an update-check result. Always non-None after a check; the
    `status` field tells the caller what happened."""

    status: str  # "current" | "available" | "no_releases" | "rate_limited" | "error"
    current_version: str
    latest_version: str | None = None
    release_url: str | None = None
    release_notes: str | None = None
    published_at: datetime | None = None
    error_detail: str | None = None
    checked_at: datetime | None = None

    @property
    def update_available(self) -> bool:
        return self.status == "available"


# In-memory cache shared across the process. Reset on daemon restart.
_cached: UpdateInfo | None = None
_cached_at: float = 0.0


def _parse_version(raw: str) -> tuple[int, ...]:
    """Parse 'v0.1.0' / '0.1.0' / '0.1.0-rc1' → comparable tuple.

    Returns () for anything that doesn't parse — caller treats unknown as
    "can't compare, assume current" so we never falsely claim an update.
    """
    v = raw.lstrip("vV").strip()
    if not v:
        return ()
    head = v.split("-")[0]  # drop pre-release suffix for the numeric compare
    parts = head.split(".")
    out: list[int] = []
    for p in parts[:3]:
        try:
            out.append(int(p))
        except ValueError:
            return ()
    return tuple(out)


def _cached_or_fresh() -> UpdateInfo | None:
    """Return the cached UpdateInfo if still within TTL, else None."""
    if _cached is None:
        return None
    if (time.monotonic() - _cached_at) > CACHE_TTL_SEC:
        return None
    return _cached


def cached() -> UpdateInfo | None:
    """Public accessor — returns the last cache value (even if stale) or None."""
    return _cached


def check_for_updates(force: bool = False) -> UpdateInfo:
    """Hit the GitHub Releases API and return current vs latest comparison.

    Honors the 1-hour memory cache unless `force=True`. Always returns an
    UpdateInfo — the `status` field tells the caller what happened (the
    daemon should never crash because the network is down).
    """
    global _cached, _cached_at
    if not force:
        cached_result = _cached_or_fresh()
        if cached_result is not None:
            return cached_result

    info = UpdateInfo(
        status="error",
        current_version=CURRENT_VERSION,
        checked_at=datetime.now(UTC),
    )
    try:
        resp = httpx.get(
            GITHUB_RELEASES_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"DriveForge/{CURRENT_VERSION}",
            },
            timeout=HTTP_TIMEOUT_SEC,
        )
    except httpx.RequestError as exc:
        info.error_detail = f"network: {exc}"
        logger.warning("update check failed: %s", exc)
        _cached, _cached_at = info, time.monotonic()
        return info

    if resp.status_code == 404:
        # No releases published yet — common during pre-alpha.
        info.status = "no_releases"
        info.error_detail = "no GitHub releases published yet"
    elif resp.status_code == 403:
        # GitHub rate limit (60 req/hr unauthenticated). Rare in practice
        # given our 1-hour cache.
        info.status = "rate_limited"
        info.error_detail = "GitHub API rate limit hit (try again in an hour)"
    elif resp.status_code != 200:
        info.error_detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
    else:
        try:
            data = resp.json()
            info.latest_version = data.get("tag_name") or data.get("name")
            info.release_url = data.get("html_url")
            info.release_notes = data.get("body")
            published = data.get("published_at")
            if published:
                # GitHub uses RFC3339 with trailing Z
                info.published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
            current = _parse_version(CURRENT_VERSION)
            latest = _parse_version(info.latest_version or "")
            if not current or not latest:
                # Couldn't parse one of them — don't claim an update.
                info.status = "current"
                info.error_detail = "version string unparseable; treating as current"
            elif latest > current:
                info.status = "available"
            else:
                info.status = "current"
        except (ValueError, KeyError) as exc:
            info.error_detail = f"malformed response: {exc}"

    _cached, _cached_at = info, time.monotonic()
    return info


def update_command() -> str:
    """The exact shell snippet to run on the host for a manual update.

    Surfaced in the Settings panel so the operator can copy-paste it into
    an SSH session — we don't run it from the web UI for security reasons
    (see the discussion in the chat history; daemon-self-update needs a
    polkit-gated systemd unit + batch refusal + reconnect logic, deferred
    as a follow-on feature).

    Probes common install locations so the snippet points at the right
    source tree. ISO-installed hosts have /opt/driveforge-src (cloned
    by preseed late_command); hosts installed via the README "Path B"
    clone-and-run flow typically have ~driveforge/driveforge-src.
    Falls back to the latter when neither exists (user is mid-install
    or the daemon was installed from elsewhere).
    """
    import os

    for candidate in ("/opt/driveforge-src", "/home/driveforge/driveforge-src"):
        if os.path.isdir(os.path.join(candidate, ".git")):
            return f"cd {candidate} && sudo git pull && sudo ./scripts/install.sh"
    return "cd /home/driveforge/driveforge-src && sudo git pull && sudo ./scripts/install.sh"


UPDATE_LOG_PATH = "/var/log/driveforge-update.log"
UPDATE_SERVICE = "driveforge-update.service"


def trigger_in_app_update() -> tuple[bool, str]:
    """Fire `sudo systemctl start driveforge-update.service` to kick off
    a self-update. Returns (ok, message).

    The unit's ExecStart runs `/usr/local/sbin/driveforge-update`, which
    git-pulls + reruns install.sh + restarts driveforge-daemon. The
    daemon's HTTP listener disappears for ~10-15 sec while that restart
    happens — the dashboard UI handles reconnect.

    Refusal cases are checked by the caller (HTTP route), not here —
    this function is the bare "fire the systemd unit" primitive. Caller
    must verify no in-flight pipeline first, since the daemon restart
    will kill any active drive's pipeline task.
    """
    import shutil
    import subprocess

    # systemctl path varies (some distros: /usr/bin, others: /bin). Use
    # the resolver so a PATH-trimmed systemd-managed environment finds it.
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    sudo = shutil.which("sudo") or "/usr/bin/sudo"
    argv = [sudo, "-n", systemctl, "start", UPDATE_SERVICE]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (False, f"failed to invoke {' '.join(argv)}: {exc}")
    if proc.returncode != 0:
        # Most common cause: sudoers rule missing or mis-installed.
        # Surface stderr verbatim so the operator sees the real reason.
        detail = (proc.stderr or proc.stdout or "").strip() or "non-zero exit"
        return (
            False,
            f"systemctl start {UPDATE_SERVICE} failed (rc={proc.returncode}): {detail}",
        )
    return (True, f"{UPDATE_SERVICE} started; live log streaming below.")


def update_log_tail(*, max_lines: int = 200) -> str:
    """Return the last `max_lines` of /var/log/driveforge-update.log.

    Empty string if the log doesn't exist (no update has ever been
    triggered on this host). Empty string also if the daemon can't read
    it — surface that as "no log available" in the UI rather than
    erroring, since a missing log is a failure mode the operator can
    investigate at the shell.
    """
    from collections import deque
    from pathlib import Path

    p = Path(UPDATE_LOG_PATH)
    if not p.exists():
        return ""
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max_lines)
        return "".join(tail)
    except OSError:
        return ""


def update_service_state() -> str:
    """Return the current systemd activity state of driveforge-update.service.

    One of: "active" (running), "activating" (starting), "inactive"
    (idle, last run completed cleanly), "failed" (last run errored),
    "unknown" (systemctl unavailable or unit not installed). The UI
    uses this to decide whether to keep polling the live log or stop.
    """
    import shutil
    import subprocess

    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    try:
        proc = subprocess.run(
            [systemctl, "is-active", UPDATE_SERVICE],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "unknown"
    # `is-active` exits 0 for active, non-zero otherwise but still prints
    # the textual state on stdout. Use stdout, not exit code.
    state = (proc.stdout or "").strip().lower()
    if state in {"active", "activating", "inactive", "failed", "deactivating"}:
        return state
    return "unknown"


def ssh_update_command() -> str:
    """One-liner that SSHes into this server and runs the update inline.

    Intended for operators who aren't already shelled in — copy-paste
    into their local terminal and be prompted for the SSH password
    (default `driveforge` from the preseed; change via `passwd` after
    first login). The `-t` flag forces SSH to allocate a TTY so the
    remote sudo prompts work inline.

    Prefers a direct LAN IP over mDNS since `.local` resolution isn't
    universally reliable across client OSes (some VPNs / corporate
    networks block multicast). Falls back to `driveforge.local` when
    the egress-IP probe can't identify a primary address.
    """
    import socket

    ip: str | None = None
    try:
        # Same egress-IP trick the setup wizard uses — no packet sent.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    target = ip or f"{socket.gethostname()}.local"
    return f"ssh -t forge@{target} '{update_command()}'"
