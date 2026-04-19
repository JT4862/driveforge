"""Subprocess runner with fixture-mode interception.

All system commands (smartctl, hdparm, sg_format, nvme, badblocks, ipmitool)
go through `run()`. In dev mode with fixtures, `run()` returns canned output
instead of invoking the real binary, letting the full pipeline be exercised
on macOS without real drives.

Callers may pass `owner=<drive_serial>` to register the spawned subprocess
so that `kill_owner()` can terminate every outstanding subprocess belonging
to that drive on abort. Without this, asyncio task cancellation can leave
sync subprocesses orphaned — particularly bad for destructive operations
like sg_format, which can corrupt the drive if interrupted mid-flight.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


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

# Registry of in-flight subprocesses keyed by an arbitrary owner tag
# (typically drive serial). Used to kill orphan subprocesses on abort.
_ACTIVE: dict[str, list[int]] = {}
_ACTIVE_LOCK = threading.Lock()


def set_fixture_runner(runner: FixtureRunner | None) -> None:
    global _FIXTURE_RUNNER
    _FIXTURE_RUNNER = runner


def _register(owner: str, pid: int) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.setdefault(owner, []).append(pid)


def _unregister(owner: str, pid: int) -> None:
    with _ACTIVE_LOCK:
        pids = _ACTIVE.get(owner)
        if pids and pid in pids:
            pids.remove(pid)
        if pids is not None and not pids:
            _ACTIVE.pop(owner, None)


def active_pids(owner: str) -> list[int]:
    with _ACTIVE_LOCK:
        return list(_ACTIVE.get(owner, []))


def kill_owner(owner: str, *, grace_sec: float = 3.0) -> int:
    """SIGTERM every subprocess tagged with `owner`, wait `grace_sec`, then SIGKILL.

    Returns the number of PIDs signalled.
    """
    pids = active_pids(owner)
    if not pids:
        return 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            logger.warning("SIGTERM pid=%d owner=%s", pid, owner)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.error("no permission to signal pid=%d", pid)
    time.sleep(grace_sec)
    for pid in pids:
        try:
            os.kill(pid, 0)  # still alive?
        except ProcessLookupError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            logger.warning("SIGKILL pid=%d owner=%s (did not exit gracefully)", pid, owner)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    return len(pids)


def run(
    argv: list[str],
    *,
    check: bool = False,
    timeout: float | None = None,
    owner: str | None = None,
) -> ProcessResult:
    """Run a command synchronously. Uses fixtures if a runner is configured.

    Pass `owner=<tag>` to register the subprocess for `kill_owner(tag)` to
    terminate it on abort.
    """
    if _FIXTURE_RUNNER is not None:
        fixture = _FIXTURE_RUNNER.lookup(argv)
        if fixture is not None:
            if check and not fixture.ok:
                raise subprocess.CalledProcessError(fixture.returncode, argv, fixture.stdout, fixture.stderr)
            return fixture
    # Use Popen instead of run() so we can register the PID for abort.
    proc = subprocess.Popen(  # noqa: S603
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if owner is not None:
        _register(owner, proc.pid)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        if owner is not None:
            _unregister(owner, proc.pid)
    result = ProcessResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if check and not result.ok:
        raise subprocess.CalledProcessError(proc.returncode, argv, stdout, stderr)
    return result


async def run_async(
    argv: list[str],
    *,
    timeout: float | None = None,
    owner: str | None = None,
) -> ProcessResult:
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
    if owner is not None and proc.pid is not None:
        _register(owner, proc.pid)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    finally:
        if owner is not None and proc.pid is not None:
            _unregister(owner, proc.pid)
    return ProcessResult(
        argv=argv,
        returncode=proc.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def pretty(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
