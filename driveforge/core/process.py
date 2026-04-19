"""Subprocess runner with fixture-mode interception.

All system commands (smartctl, hdparm, sg_format, nvme, badblocks, ipmitool)
go through `run()`. In dev mode with fixtures, `run()` returns canned output
instead of invoking the real binary, letting the full pipeline be exercised
on macOS without real drives.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class FixtureRunner:
    """Loads canned command output from a fixtures directory.

    Directory layout: `<fixtures_dir>/<binary>/<hash>.stdout` etc. The hash is
    a stable digest of the argv so multiple test invocations of the same
    command return the same fixture. A simple `_default.stdout` can act as a
    catch-all during early development.
    """

    def __init__(self, fixtures_dir: Path) -> None:
        self.fixtures_dir = fixtures_dir

    def lookup(self, argv: list[str]) -> ProcessResult | None:
        if not argv:
            return None
        binary = Path(argv[0]).name
        bin_dir = self.fixtures_dir / binary
        if not bin_dir.exists():
            return None
        # Fixture naming: join the non-path args, slash → underscore
        args_key = "_".join(a.replace("/", "_") for a in argv[1:]) or "_default"
        stdout_file = bin_dir / f"{args_key}.stdout"
        if not stdout_file.exists():
            stdout_file = bin_dir / "_default.stdout"
            if not stdout_file.exists():
                return None
        stderr_file = stdout_file.with_suffix(".stderr")
        rc_file = stdout_file.with_suffix(".rc")
        return ProcessResult(
            argv=argv,
            returncode=int(rc_file.read_text().strip()) if rc_file.exists() else 0,
            stdout=stdout_file.read_text(),
            stderr=stderr_file.read_text() if stderr_file.exists() else "",
        )


_FIXTURE_RUNNER: FixtureRunner | None = None


def set_fixture_runner(runner: FixtureRunner | None) -> None:
    global _FIXTURE_RUNNER
    _FIXTURE_RUNNER = runner


def run(argv: list[str], *, check: bool = False, timeout: float | None = None) -> ProcessResult:
    """Run a command synchronously. Uses fixtures if a runner is configured."""
    if _FIXTURE_RUNNER is not None:
        fixture = _FIXTURE_RUNNER.lookup(argv)
        if fixture is not None:
            if check and not fixture.ok:
                raise subprocess.CalledProcessError(fixture.returncode, argv, fixture.stdout, fixture.stderr)
            return fixture
    completed = subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )
    return ProcessResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


async def run_async(argv: list[str], *, timeout: float | None = None) -> ProcessResult:
    """Run a command asynchronously. Uses fixtures if a runner is configured."""
    if _FIXTURE_RUNNER is not None:
        fixture = _FIXTURE_RUNNER.lookup(argv)
        if fixture is not None:
            return fixture
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return ProcessResult(
        argv=argv,
        returncode=proc.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def pretty(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
