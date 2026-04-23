"""`driveforge` CLI — admin operations and one-shot helpers.

Primary UI is the web app; this CLI exists for scripters, support, and cases
where the daemon isn't running.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from driveforge import config as cfg
from driveforge.core import drive as drive_mod
from driveforge.version import __version__


@click.group()
@click.version_option(__version__, prog_name="driveforge")
def main() -> None:
    """DriveForge — enterprise drive refurbishment pipeline."""


@main.command()
def discover() -> None:
    """List attached drives (excluding the OS disk)."""
    drives = drive_mod.discover()
    if not drives:
        click.echo("No drives discovered.")
        return
    for d in drives:
        click.echo(
            f"{d.device_path:<16} {d.transport.value:<6} {d.serial:<24} "
            f"{d.model:<32} {d.capacity_tb:>6.2f} TB"
        )


@main.group()
def config() -> None:
    """Inspect / set daemon config."""


@config.command("get")
@click.option("--path", type=click.Path(path_type=Path), default=None)
def config_get(path: Path | None) -> None:
    settings = cfg.load(path)
    click.echo(json.dumps(settings.model_dump(mode="json"), indent=2, default=str))


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--path", type=click.Path(path_type=Path), default=None)
def config_set(key: str, value: str, path: Path | None) -> None:
    """Set a dotted key, e.g. `driveforge config set daemon.port 8080`."""
    settings = cfg.load(path)
    cur: object = settings
    parts = key.split(".")
    for part in parts[:-1]:
        cur = getattr(cur, part)
    try:
        current_value = getattr(cur, parts[-1])
    except AttributeError:
        click.echo(f"unknown key: {key}", err=True)
        sys.exit(2)
    # Coerce to the existing type where possible
    coerced: object = value
    if isinstance(current_value, bool):
        coerced = value.lower() in {"1", "true", "yes", "on"}
    elif isinstance(current_value, int):
        coerced = int(value)
    elif isinstance(current_value, float):
        coerced = float(value)
    setattr(cur, parts[-1], coerced)
    cfg.save(settings, path)
    click.echo(f"set {key} = {coerced}")


@main.command()
def version() -> None:
    click.echo(__version__)


# ---------------------------------------------------------------- fleet
#
# v0.10.0+. Agent-side bootstrap + status. The operator side is driven
# from the web UI (Settings → Agents); this CLI group is what runs on
# the worker boxes that are joining a fleet.


@main.group()
def fleet() -> None:
    """Multi-node fleet commands (v0.10.0+)."""


@fleet.command("status")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
def fleet_status(config_path: Path | None) -> None:
    """Show this node's fleet role + related config."""
    settings = cfg.load(config_path)
    fleet_cfg = settings.fleet
    click.echo(f"role:            {fleet_cfg.role}")
    click.echo(f"display_name:    {fleet_cfg.display_name or '(uses hostname)'}")
    if fleet_cfg.role == "agent":
        click.echo(f"operator_url:    {fleet_cfg.operator_url or '(unset)'}")
        click.echo(f"api_token_path:  {fleet_cfg.api_token_path}")
        from driveforge.core import fleet as fleet_mod
        token = fleet_mod.read_agent_token(fleet_cfg.api_token_path)
        click.echo(f"token present:   {'yes' if token else 'no'}")
    elif fleet_cfg.role == "operator":
        click.echo(f"listen_port:     {fleet_cfg.listen_port}")


@fleet.command("join")
@click.argument("operator_url")
@click.argument("token")
@click.option(
    "--display-name",
    default=None,
    help="Friendly name for this agent on the operator's dashboard. Defaults to hostname.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to driveforge.yaml (defaults to /etc/driveforge/driveforge.yaml).",
)
@click.option(
    "--no-restart",
    is_flag=True,
    default=False,
    help="Skip the systemctl restart of driveforge-daemon after saving.",
)
def fleet_join(
    operator_url: str,
    token: str,
    display_name: str | None,
    config_path: Path | None,
    no_restart: bool,
) -> None:
    """Enroll this node as an agent in a DriveForge fleet.

    OPERATOR_URL is the base URL of the operator's daemon (e.g.
    https://nx3200.local:8080). TOKEN is a one-shot enrollment token
    generated on the operator via Settings → Agents → Generate
    enrollment token.

    On success, writes the long-lived agent token to the path in
    `fleet.api_token_path` (default /etc/driveforge/agent.token, mode
    600), flips this daemon's role to "agent", records
    operator_url in the config, and restarts the daemon service so
    agent mode takes effect.

    Usage:
      sudo driveforge fleet join https://nx3200.local:8080 abc123.xyz789...
    """
    import socket
    import subprocess
    from urllib.parse import urlparse

    import httpx
    from driveforge.core import fleet as fleet_mod

    # Normalize the operator URL — strip trailing slash, default to
    # https if the user pasted host:port with no scheme. Accept both
    # http:// and https://.
    op = operator_url.strip().rstrip("/")
    if "://" not in op:
        op = f"https://{op}"
    parsed = urlparse(op)
    if parsed.scheme not in {"http", "https"}:
        click.echo(f"operator URL must be http:// or https://, got {parsed.scheme}://", err=True)
        sys.exit(2)

    hostname = socket.gethostname()
    effective_display = display_name or hostname

    enroll_url = f"{op}/api/fleet/enroll"
    click.echo(f"Enrolling with operator at {op} as '{effective_display}'...")
    try:
        # verify=False is a homelab-pragmatic choice: operators on
        # LAN-local mDNS hostnames (nx3200.local) won't have a
        # trusted TLS cert, and mTLS/cert-pinning is v0.10.5 scope.
        # The enrollment token itself is the trust anchor here.
        resp = httpx.post(
            enroll_url,
            json={
                "token": token,
                "display_name": effective_display,
                "hostname": hostname,
                "version": __version__,
            },
            timeout=15.0,
            verify=False,
        )
    except httpx.RequestError as exc:
        click.echo(f"failed to reach operator: {exc}", err=True)
        sys.exit(1)
    if resp.status_code != 200:
        # Surface the operator's error detail so the operator running
        # the enrollment knows whether their token expired, is
        # malformed, etc.
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        click.echo(f"enrollment rejected ({resp.status_code}): {detail}", err=True)
        sys.exit(1)

    body = resp.json()
    agent_id = body["agent_id"]
    api_token = body["api_token"]
    operator_version = body.get("operator_version", "unknown")

    # Load existing settings, flip to agent mode, save.
    settings = cfg.load(config_path)
    settings.fleet.role = "agent"
    settings.fleet.operator_url = op
    if display_name:
        settings.fleet.display_name = display_name
    token_path = settings.fleet.api_token_path
    try:
        fleet_mod.write_agent_token(token_path, api_token)
    except PermissionError as exc:
        click.echo(
            f"cannot write token to {token_path}: {exc}\n"
            "Try running with sudo, or pass --config for a writable location.",
            err=True,
        )
        sys.exit(1)
    cfg.save(settings, config_path)

    click.echo(f"enrolled as agent {agent_id} (operator v{operator_version})")
    click.echo(f"token written to {token_path}")

    if no_restart:
        click.echo("Skipping daemon restart (--no-restart). Restart manually:")
        click.echo("  sudo systemctl restart driveforge-daemon")
        return

    # Restart the daemon so agent mode takes effect. On non-systemd
    # systems (dev, macOS) this will fail silently — the user runs
    # the daemon manually there anyway.
    try:
        subprocess.run(
            ["systemctl", "restart", "driveforge-daemon"],
            check=True,
            timeout=30,
        )
        click.echo("daemon restarted")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        click.echo(
            f"note: could not restart daemon automatically ({exc}).\n"
            "Restart manually when convenient: sudo systemctl restart driveforge-daemon",
            err=True,
        )


@fleet.command("leave")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--no-restart", is_flag=True, default=False)
@click.confirmation_option(
    prompt="Detach from the fleet and revert to standalone mode?"
)
def fleet_leave(config_path: Path | None, no_restart: bool) -> None:
    """Detach this agent from its fleet; revert to standalone mode.

    Clears `fleet.role` back to "standalone", removes the operator
    URL + agent token. The operator's Agent row stays in its DB for
    history purposes — the operator should click Revoke on their
    Settings → Agents page to fully clean up.
    """
    import subprocess
    settings = cfg.load(config_path)
    if settings.fleet.role != "agent":
        click.echo(f"not an agent (current role: {settings.fleet.role}); nothing to do")
        return
    token_path = settings.fleet.api_token_path
    settings.fleet.role = "standalone"
    settings.fleet.operator_url = None
    cfg.save(settings, config_path)
    if token_path.exists():
        try:
            token_path.unlink()
        except OSError as exc:
            click.echo(f"warning: could not remove {token_path}: {exc}", err=True)
    click.echo("reverted to standalone mode")
    if not no_restart:
        try:
            subprocess.run(
                ["systemctl", "restart", "driveforge-daemon"],
                check=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            pass


if __name__ == "__main__":
    main()
