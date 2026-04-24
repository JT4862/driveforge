"""Fleet discovery via avahi/mDNS (v0.11.0+).

Candidates advertise themselves on the LAN; operators browse for them.
Both sides shell out to `avahi-publish-service` / `avahi-browse`
already installed via `avahi-utils` (our apt list includes it as of
v0.1.x — the `ledctl` workflow depended on it).

Why subprocess rather than a Python mDNS lib:
- Already installed on every DriveForge host. No new PyPI dep.
- avahi-daemon is the mDNS responder regardless of who's publishing;
  `avahi-publish-service` just registers one more service with it.
- Transparent to debug: `avahi-browse -r` on any box on the LAN
  shows exactly what candidates are advertising.

Service type: `_driveforge-candidate._tcp`
TXT record keys:
  - version=<driveforge semver>
  - hostname=<candidate's short hostname>
  - install_id=<12-hex random>          # stable across reboots
  - mac_suffix=<6-hex from primary NIC> # for uniquify-check parity

The port in the SRV record is the candidate's web port (8080). We
don't actually USE this port on the operator side — the adoption
POST goes to the candidate's resolved hostname + port — but mDNS
requires a port, so we publish the real one.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CANDIDATE_SERVICE_TYPE = "_driveforge-candidate._tcp"
OPERATOR_SERVICE_TYPE = "_driveforge-operator._tcp"

# How often the operator re-browses the LAN for candidates. Candidates
# re-advertise continuously via avahi-daemon so the browse is cheap;
# 15 s gives the operator's "Discovered" panel a responsive feel
# without burning CPU.
BROWSE_INTERVAL_S = 15.0
BROWSE_TIMEOUT_S = 5.0


@dataclass
class DiscoveredCandidate:
    """One row in the operator's "Discovered on network" panel."""
    install_id: str
    hostname: str
    version: str
    address: str  # IP address resolved from mDNS
    port: int
    last_seen_monotonic: float
    # Operator-set: "ignored" candidates stay in the state dict for
    # deduping the browse output but don't render on the Discovered
    # panel.
    ignored: bool = False


def ensure_install_id(settings: Any, config_path: Any = None) -> str:
    """v0.11.0+ — lazily mint a per-install random ID + persist.

    Generated once on first need; stable across reboots. Used in the
    candidate's mDNS advertisement so operator-side dedup + identity
    doesn't depend on hostname (two identical bare-metal servers with
    the same MAC-derived hostname would otherwise look the same).
    """
    if settings.fleet.install_id:
        return settings.fleet.install_id
    from driveforge import config as cfg
    install_id = secrets.token_hex(6)  # 12 hex chars
    settings.fleet.install_id = install_id
    try:
        cfg.save(settings, config_path)
    except PermissionError:
        logger.debug("install_id minted in-memory only (config dir not writable)")
    return install_id


def ensure_fleet_id(settings: Any, config_path: Any = None) -> str:
    """v0.11.0+ — lazy-generate the operator's fleet identifier."""
    if settings.fleet.fleet_id:
        return settings.fleet.fleet_id
    from driveforge import config as cfg
    fleet_id = secrets.token_hex(8)  # 16 hex chars
    settings.fleet.fleet_id = fleet_id
    try:
        cfg.save(settings, config_path)
    except PermissionError:
        logger.debug("fleet_id minted in-memory only (config dir not writable)")
    return fleet_id


class _PublishProcess:
    """Wrapper around `avahi-publish-service` subprocess. Kills
    cleanly on cancellation so cycling between roles doesn't leave
    stray advertisements on the LAN."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def start(
        self,
        *,
        name: str,
        service_type: str,
        port: int,
        txt_records: dict[str, str],
    ) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return  # already running
        cmd = ["avahi-publish-service", name, service_type, str(port)]
        for k, v in txt_records.items():
            cmd.append(f"{k}={v}")
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("fleet-discovery: publishing %s as '%s'", service_type, name)
        except FileNotFoundError:
            logger.warning(
                "fleet-discovery: avahi-publish-service not installed; "
                "candidate will not appear on operator's Discovered list"
            )
            self._proc = None

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except OSError:
            pass
        self._proc = None


async def candidate_publish_loop(state: Any) -> None:
    """Long-running task: publish this box as a candidate until
    role changes. One avahi-publish subprocess per run."""
    if state.settings.fleet.role != "candidate":
        return
    settings = state.settings
    install_id = ensure_install_id(settings)

    import socket as _socket
    hostname = _socket.gethostname().split(".")[0]
    from driveforge.version import __version__ as version

    publisher = _PublishProcess()
    try:
        publisher.start(
            name=f"DriveForge {hostname}",
            service_type=CANDIDATE_SERVICE_TYPE,
            port=settings.daemon.port,
            txt_records={
                "version": version,
                "hostname": hostname,
                "install_id": install_id,
            },
        )
        # Sleep until the daemon shuts us down.
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise
    finally:
        publisher.stop()


async def operator_discover_loop(state: Any) -> None:
    """Long-running task: periodically browse the LAN for
    candidates and update state.discovered_candidates.

    Uses `avahi-browse -rpt` (resolve all, parseable, terminate after
    cache exhausted) which returns a ~5-second snapshot of every
    candidate currently advertising. Between runs we sleep
    BROWSE_INTERVAL_S; candidates that drop off the LAN are purged
    after 2× that interval.
    """
    if state.settings.fleet.role != "operator":
        return
    import time as _time
    while True:
        try:
            await _run_one_browse(state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("fleet-discovery: browse iteration failed")
        # Purge stale
        now = _time.monotonic()
        stale_cutoff = now - (BROWSE_INTERVAL_S * 2)
        for cid in list(state.discovered_candidates.keys()):
            if state.discovered_candidates[cid].last_seen_monotonic < stale_cutoff:
                del state.discovered_candidates[cid]
        try:
            await asyncio.sleep(BROWSE_INTERVAL_S)
        except asyncio.CancelledError:
            raise


async def _run_one_browse(state: Any) -> None:
    """Run avahi-browse once and merge results into
    state.discovered_candidates."""
    import time as _time

    cmd = [
        "avahi-browse",
        "-r",    # resolve addresses
        "-p",    # parseable output
        "-t",    # terminate after cache exhausted
        CANDIDATE_SERVICE_TYPE,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # No avahi-browse; operator can't discover. Log once per
        # daemon lifetime.
        if not getattr(state, "_avahi_browse_missing_warned", False):
            logger.warning(
                "fleet-discovery: avahi-browse not installed; "
                "candidate auto-discovery disabled"
            )
            state._avahi_browse_missing_warned = True  # type: ignore[attr-defined]
        return

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=BROWSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return

    now = _time.monotonic()
    for line in stdout.decode(errors="replace").splitlines():
        parsed = _parse_avahi_line(line)
        if parsed is None:
            continue
        install_id = parsed.get("install_id")
        if not install_id:
            # Shouldn't happen for a well-formed candidate; skip.
            continue
        existing = state.discovered_candidates.get(install_id)
        ignored = existing.ignored if existing else False
        state.discovered_candidates[install_id] = DiscoveredCandidate(
            install_id=install_id,
            hostname=parsed.get("hostname", "?"),
            version=parsed.get("version", "?"),
            address=parsed.get("address", "?"),
            port=parsed.get("port", 8080),
            last_seen_monotonic=now,
            ignored=ignored,
        )


def _parse_avahi_line(line: str) -> dict | None:
    """Parse one line of `avahi-browse -rpt` output.

    The relevant lines start with '=' (resolved service) and have
    semicolon-separated fields:
        =;iface;proto;name;type;domain;hostname;address;port;txt

    txt is space-separated `key=value` tokens wrapped in quotes.

    Example:
        =;enp5s0;IPv4;DriveForge\\032foo;_driveforge-candidate._tcp;local;
        foo.local;10.0.0.5;8080;"version=0.11.0" "hostname=foo" "install_id=abc123"
    """
    if not line.startswith("="):
        return None
    fields = line.split(";")
    if len(fields) < 10:
        return None
    hostname = fields[6] or "?"
    # Strip trailing .local that avahi includes in the FQDN.
    if hostname.endswith(".local"):
        hostname = hostname[: -len(".local")]
    address = fields[7] or "?"
    try:
        port = int(fields[8])
    except (TypeError, ValueError):
        port = 8080
    txt_raw = fields[9] if len(fields) > 9 else ""
    # txt_raw is quoted space-separated key=val tokens. Crude parse:
    # split on `" "` after stripping outer quotes.
    txt: dict[str, str] = {}
    for token in _split_quoted(txt_raw):
        if "=" in token:
            k, _, v = token.partition("=")
            txt[k.strip()] = v.strip()
    txt["hostname"] = txt.get("hostname") or hostname
    txt["address"] = address
    txt["port"] = port  # type: ignore[assignment]
    return txt


def _split_quoted(s: str) -> list[str]:
    """Extract `foo=bar` tokens from avahi's quoted TXT output."""
    out: list[str] = []
    in_quote = False
    cur = ""
    for ch in s:
        if ch == '"':
            if in_quote:
                if cur:
                    out.append(cur)
                cur = ""
                in_quote = False
            else:
                in_quote = True
                cur = ""
        elif in_quote:
            cur += ch
    return out
