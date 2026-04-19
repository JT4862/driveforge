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


if __name__ == "__main__":
    main()
