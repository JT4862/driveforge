"""Hostname read + rename.

v0.2.8. The problem: `iso/preseed.cfg` hardcodes the target's hostname
to `driveforge`, so a second DriveForge box on the same LAN gets
auto-suffixed by avahi — `driveforge-2.local`, `driveforge-3.local` —
and which box wins `driveforge.local` is non-deterministic across
reboots. For single-box installs the default is fine; for multi-box
deployments the operator needs a way to rename a box after install
without reflashing USB.

This module provides the read + validate + apply primitives. The apply
path writes `/etc/hostname`, calls `hostnamectl set-hostname`, and
reloads avahi-daemon so mDNS re-publishes under the new name without
a reboot. `/etc/hosts` is patched to keep the 127.0.1.1 loopback row
pointing at the new hostname (standard Debian layout — breaking this
makes `sudo` log DNS warnings).

The Settings UI calls `apply_hostname()` from a POST handler. Dev mode
(no sudo / non-root) is a silent no-op for the system-level writes so
tests on macOS don't need a real /etc or hostnamectl.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from pathlib import Path

from driveforge.core.process import run

logger = logging.getLogger(__name__)

# RFC 1123 / Debian hostname rules:
#   - 1 to 63 characters per label
#   - ASCII letters, digits, hyphens only
#   - Must not start or end with a hyphen
#   - Must not be all-numeric (debian convention, avoids IP-address confusion)
# We enforce a SINGLE label (no dots). Users who want FQDNs can configure
# /etc/hosts manually — our avahi .local publishing only needs the short name.
_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

# Reserve names whose use would break the system: `localhost` aliases
# 127.0.0.1 in /etc/hosts, `driveforge` is the daemon user account name
# (reusing it as the hostname is legal but confusing on multi-box LANs —
# the whole point of this feature is to NOT default every box to that).
# We don't ban `driveforge`, just nudge against it in the UI copy.
_RESERVED = {"localhost", "localdomain", "ip6-localhost", "ip6-loopback"}


class HostnameError(ValueError):
    """Raised for invalid hostname input or when the system rename fails."""


def current_hostname() -> str:
    """Best-effort current hostname. Reads /etc/hostname if present,
    otherwise falls back to `socket.gethostname()`.

    Trims whitespace. Returns "" if everything fails — the caller can
    render that as "unknown" in the UI.
    """
    etc = Path("/etc/hostname")
    try:
        if etc.exists():
            name = etc.read_text(encoding="utf-8").strip()
            if name:
                return name
    except OSError:
        pass
    try:
        return socket.gethostname().split(".")[0]
    except OSError:
        return ""


def validate_hostname(name: str) -> str:
    """Normalize + validate. Returns the normalized name on success,
    raises HostnameError on failure with a user-facing message."""
    if name is None:
        raise HostnameError("Hostname is required")
    normalized = name.strip().lower()
    if not normalized:
        raise HostnameError("Hostname is required")
    if len(normalized) > 63:
        raise HostnameError("Hostname must be 63 characters or fewer")
    if normalized.isdigit():
        raise HostnameError("Hostname must not be all digits")
    if normalized in _RESERVED:
        raise HostnameError(f"'{normalized}' is a reserved name")
    if not _LABEL_RE.match(normalized):
        raise HostnameError(
            "Hostname must contain only letters, digits, and hyphens, "
            "and must not start or end with a hyphen"
        )
    return normalized


def apply_hostname(new_name: str, *, dev_mode: bool = False) -> str:
    """Validate, then rename the system.

    Steps (all idempotent):
      1. Write `/etc/hostname` atomically.
      2. `hostnamectl set-hostname <name>` so the kernel + systemd pick it up.
      3. Patch `/etc/hosts`: keep the 127.0.1.1 row pointing at the new
         name. If the row is absent we add one; if it's there with an
         old value we rewrite it. Leaves the 127.0.0.1 localhost row
         alone.
      4. Restart `avahi-daemon` so mDNS re-publishes under the new name.
         Without this the box keeps advertising the OLD hostname until
         the next service restart or reboot.

    Returns the normalized name that was applied. Raises HostnameError
    on validation failure OR on a subprocess failure (the error carries
    enough detail for the Settings UI to surface).

    In dev mode everything except validation is a no-op — lets the test
    suite exercise the route without needing root or a real
    hostnamectl binary.
    """
    normalized = validate_hostname(new_name)
    if normalized == current_hostname():
        logger.info("apply_hostname: new name equals current (%s); no-op", normalized)
        return normalized

    if dev_mode:
        logger.info("apply_hostname(dev): would set hostname to %s", normalized)
        return normalized

    # 1. /etc/hostname — atomic write via tmpfile + rename.
    etc_hostname = Path("/etc/hostname")
    try:
        tmp = etc_hostname.with_suffix(".new")
        tmp.write_text(normalized + "\n", encoding="utf-8")
        os.replace(tmp, etc_hostname)
    except OSError as exc:
        raise HostnameError(f"Failed to write /etc/hostname: {exc}") from exc

    # 2. hostnamectl — tells systemd + kernel the new name.
    result = run(["hostnamectl", "set-hostname", normalized], timeout=10)
    if not result.ok:
        raise HostnameError(
            f"hostnamectl set-hostname failed: "
            f"{(result.stderr or result.stdout or '').strip() or 'unknown error'}"
        )

    # 3. /etc/hosts — keep 127.0.1.1 row in sync.
    try:
        _patch_etc_hosts(normalized)
    except OSError as exc:
        # Non-fatal: the system is renamed, just log the warning.
        logger.warning("could not patch /etc/hosts: %s", exc)

    # 4. avahi-daemon reload (restart is more reliable than reload for
    # hostname republish — avahi sometimes caches the old name on reload).
    avahi = run(["systemctl", "restart", "avahi-daemon.service"], timeout=15)
    if not avahi.ok:
        logger.warning(
            "avahi-daemon restart returned non-zero: %s (mDNS may take a reboot to update)",
            (avahi.stderr or avahi.stdout or "").strip(),
        )

    logger.info("hostname renamed to %s", normalized)
    return normalized


def _patch_etc_hosts(new_name: str) -> None:
    """Rewrite the 127.0.1.1 row in /etc/hosts to point at new_name.

    Preserves every other line verbatim. If no 127.0.1.1 row exists, one
    is appended. Debian convention is:
        127.0.0.1    localhost
        127.0.1.1    <hostname>
    and tools like sudo read the second row to resolve the short name.
    """
    hosts = Path("/etc/hosts")
    if not hosts.exists():
        return
    original = hosts.read_text(encoding="utf-8").splitlines()
    replaced = False
    out: list[str] = []
    for line in original:
        stripped = line.strip()
        if stripped.startswith("127.0.1.1") and not stripped.startswith("#"):
            out.append(f"127.0.1.1\t{new_name}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"127.0.1.1\t{new_name}")
    tmp = hosts.with_suffix(".new")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, hosts)
