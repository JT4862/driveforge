"""badblocks wrapper.

Runs a destructive write/read scan. Can take 24-48 hours on an 8TB HDD, so
we run async and let the orchestrator poll for completion.
"""

from __future__ import annotations

import re

from driveforge.core.process import run_async, ProcessResult


class BadblocksError(RuntimeError):
    pass


# Example progress line from badblocks -w:
# "34.56% done, 2:15:03 elapsed. (0/0/0 errors)"
_PROGRESS_RE = re.compile(
    r"(?P<pct>\d+(?:\.\d+)?)%\s+done.*?\((?P<r>\d+)/(?P<w>\d+)/(?P<c>\d+)\s+errors\)"
)


def parse_progress(line: str) -> tuple[float, tuple[int, int, int]] | None:
    """Return (percent, (read_errors, write_errors, compare_errors))."""
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    return float(m["pct"]), (int(m["r"]), int(m["w"]), int(m["c"]))


async def run_destructive(device: str, *, timeout: float = 72 * 60 * 60) -> ProcessResult:
    """Run `badblocks -w` destructively.

    Caller is responsible for ensuring the drive is out of use. Returns the
    full ProcessResult; parse `stdout` with `parse_progress()` if streaming.
    """
    # -w: destructive write-mode test
    # -s: show progress
    # -b 4096: 4k block size — sane default, override per-drive if needed
    # -v: verbose (puts progress on stderr)
    return await run_async(["badblocks", "-wsv", "-b", "4096", device], timeout=timeout)
