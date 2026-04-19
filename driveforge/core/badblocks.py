"""badblocks wrapper.

Runs a destructive write/read scan. Can take 24-48 hours on an 8TB HDD, so
the orchestrator streams progress line-by-line instead of blocking on a
single await.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from driveforge.core import process

# 1 MiB blocks with -c 32 (→ 32 MiB per pattern in-memory) — chosen so badblocks
# hits native sequential throughput on both SSDs (100-500 MB/s) and modern HDDs
# (150-250 MB/s). The old default of -b 4096 throttled SSDs badly — observed
# ~5 GB/h on an Intel SATA SSD on a SAS HBA. Do not reduce without benchmarking.
BLOCK_SIZE = 1048576
BLOCK_COUNT = 32


class BadblocksError(RuntimeError):
    pass


# Example progress line from badblocks -w:
# "34.56% done, 2:15:03 elapsed. (0/0/0 errors)"
_PROGRESS_RE = re.compile(
    r"(?P<pct>\d+(?:\.\d+)?)%\s+done.*?\((?P<r>\d+)/(?P<w>\d+)/(?P<c>\d+)\s+errors\)"
)
# Header lines that signal a new pass in `-w` mode. badblocks runs 4 patterns
# and after each writes, reads the drive back to verify, so there are 8 total
# passes: write(0xAA), verify, write(0x55), verify, write(0xFF), verify,
# write(0x00), verify. Progress % resets to 0 at the start of each pass.
_PATTERN_HDR_RE = re.compile(
    r"Testing with pattern\s+(?P<pat>0x[0-9a-fA-F]+)", re.IGNORECASE
)
_VERIFY_HDR_RE = re.compile(r"Reading and comparing", re.IGNORECASE)

TOTAL_PASSES = 8


def parse_progress(line: str) -> tuple[float, tuple[int, int, int]] | None:
    """Return (percent, (read_errors, write_errors, compare_errors))."""
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    return float(m["pct"]), (int(m["r"]), int(m["w"]), int(m["c"]))


async def run_destructive_streaming(
    device: str,
    *,
    on_progress: Callable[[float, tuple[int, int, int], str | None], None] | None = None,
    owner: str | None = None,
    timeout: float = 72 * 60 * 60,
) -> tuple[int, int, int]:
    """Run `badblocks -wsv` and stream progress via `on_progress` callback.

    The callback receives (percent, errors, pass_label). `pass_label` is a
    human-readable string like "pass 3/8 · write 0xFF" that tracks which of
    the 8 write/verify sweeps badblocks is currently running — it's None
    until the first pattern header is seen.

    Returns the final (read_errors, write_errors, compare_errors) tuple.
    Raises BadblocksError if badblocks exits non-zero. Pass `owner=<drive_serial>`
    so `process.kill_owner()` can terminate the subprocess on abort.
    """
    argv = ["badblocks", "-wsv", "-b", str(BLOCK_SIZE), "-c", str(BLOCK_COUNT), device]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if owner is not None and proc.pid is not None:
        process.register_pid(owner, proc.pid)
    errors: tuple[int, int, int] = (0, 0, 0)
    # Pass-tracking state. pass_num is 1-based (1..8); sub is "write" then
    # "verify" for each of 4 patterns. current_pattern is last pattern seen.
    state: dict[str, object] = {"pass_num": 0, "sub": "", "pattern": ""}

    def pass_label() -> str | None:
        if state["pass_num"] == 0:
            return None
        return f"pass {state['pass_num']}/{TOTAL_PASSES} · {state['sub']} {state['pattern']}"

    async def pump(stream: asyncio.StreamReader) -> None:
        nonlocal errors
        buf = b""
        while True:
            chunk = await stream.read(256)
            if not chunk:
                break
            # badblocks -s uses \b (backspace) to overwrite the progress counter
            # in place when stderr isn't a TTY; older paths use \r. Normalize
            # both to \n so updates terminate and get parsed promptly — otherwise
            # the dashboard sits at 0% until the pattern completes (hours).
            buf += chunk.replace(b"\b", b"\n").replace(b"\r", b"\n")
            while b"\n" in buf:
                idx = buf.find(b"\n")
                line = buf[:idx].decode("utf-8", errors="replace").strip()
                buf = buf[idx + 1 :]
                if not line:
                    continue
                # Each pattern or verify header starts a new pass; badblocks
                # emits exactly 8 of these in `-w` mode (4 patterns × write+verify).
                mh = _PATTERN_HDR_RE.search(line)
                if mh is not None:
                    state["pass_num"] = int(state["pass_num"]) + 1
                    state["sub"] = "write"
                    state["pattern"] = mh.group("pat").upper().replace("0X", "0x")
                    continue
                if _VERIFY_HDR_RE.search(line):
                    state["pass_num"] = int(state["pass_num"]) + 1
                    state["sub"] = "verify"
                    continue
                parsed = parse_progress(line)
                if parsed is not None:
                    pct, errs = parsed
                    errors = errs
                    if on_progress is not None:
                        try:
                            on_progress(pct, errs, pass_label())
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
    finally:
        if owner is not None and proc.pid is not None:
            process.unregister_pid(owner, proc.pid)
    rc = await proc.wait()
    if rc != 0:
        raise BadblocksError(f"badblocks exited {rc} on {device}")
    return errors
