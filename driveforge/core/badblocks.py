"""badblocks wrapper.

Runs a destructive write/read scan. Can take 24-48 hours on an 8TB HDD, so
the orchestrator streams progress line-by-line instead of blocking on a
single await.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable


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


async def run_destructive_streaming(
    device: str,
    *,
    on_progress: Callable[[float, tuple[int, int, int]], None] | None = None,
    timeout: float = 72 * 60 * 60,
) -> tuple[int, int, int]:
    """Run `badblocks -wsv` and stream progress via `on_progress` callback.

    Returns the final (read_errors, write_errors, compare_errors) tuple.
    Raises BadblocksError if badblocks exits non-zero.
    """
    argv = ["badblocks", "-wsv", "-b", "4096", device]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    errors: tuple[int, int, int] = (0, 0, 0)

    async def pump(stream: asyncio.StreamReader) -> None:
        nonlocal errors
        buf = b""
        while True:
            chunk = await stream.read(256)
            if not chunk:
                break
            buf += chunk
            # badblocks uses \r for progress updates (not \n)
            while b"\r" in buf or b"\n" in buf:
                sep = min(
                    (buf.find(b"\r") if b"\r" in buf else len(buf) + 1),
                    (buf.find(b"\n") if b"\n" in buf else len(buf) + 1),
                )
                line = buf[:sep].decode("utf-8", errors="replace")
                buf = buf[sep + 1 :]
                if not line:
                    continue
                parsed = parse_progress(line)
                if parsed is not None:
                    pct, errs = parsed
                    errors = errs
                    if on_progress is not None:
                        try:
                            on_progress(pct, errs)
                        except Exception:  # noqa: BLE001
                            pass

    try:
        await asyncio.wait_for(
            asyncio.gather(pump(proc.stdout), pump(proc.stderr)),  # type: ignore[arg-type]
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    rc = await proc.wait()
    if rc != 0:
        raise BadblocksError(f"badblocks exited {rc} on {device}")
    return errors
