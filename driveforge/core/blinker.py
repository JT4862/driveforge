"""Drive activity-LED blinker.

Runs a distinctive I/O pattern against a finished drive so its bay's
activity LED shows a clear "ready to pull" signal. The R720 LFF
direct-attach backplane exposes no SES enclosure, so per-slot IDENT/FAULT
LEDs aren't reachable — but every drive's own green activity LED is
wired to the bay, so we piggyback on that.

Two patterns, maximally distinct at a glance across the room:

- PASS (solid green): continuous 64 KB reads with no dark gap. To the
  eye the activity LED stays lit. Fires for any successful run
  regardless of grade (A / B / C — the operator pulls them all the
  same way).
- FAIL (lighthouse): 1.5 s of continuous reads (LED solid-on), then
  1.5 s of full dark. Reads as "solid ON … solid OFF, solid ON …
  solid OFF" — clearly different from a running drive's natural
  activity OR from the always-on pass pattern.

No blinker for aborted runs (operator knew they cancelled; no LED
noise needed) or for drives that have never completed a run.

For hardware with proper backplane management (SES via sg_ses, IBPI
via ledctl/ledmon — present on R720 SFF, most newer Dell/HPE/Lenovo
chassis), we also try to light the bay's amber FAULT LED via `ledctl`.
That's pure bonus signal on top of the activity pattern — no-op on the
R720 LFF and any other chassis without SGPIO wired through, so the
read-pattern still works as the universal fallback.

The blinker stops as soon as the drive is pulled (open() raises
OSError) or when the task is cancelled (new batch, daemon shutdown,
abort).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from typing import Literal

logger = logging.getLogger(__name__)

Pattern = Literal["pass", "fail"]

# Offsets to rotate through so repeated reads actually hit the device
# instead of being served from page cache OR the drive's own onboard DRAM
# cache (typically 16-64 MiB on modern HDDs). 500 offsets × 50 MiB stride
# spans ~25 GiB, well beyond any drive's internal cache, so even after
# kernel page-cache warms we're still hitting fresh platter for most reads.
_PROBE_OFFSETS = [i * (50 * 1024 * 1024) for i in range(500)]
# 64 KB per probe — takes ~25-30 ms on rotational media, short enough to
# be responsive to cancellation but long enough that consecutive reads
# merge into a visibly "solid" activity LED.
_PROBE_SIZE = 64 * 1024

# FAIL lighthouse cadence
_FAIL_ON_DURATION_SEC = 1.5
_FAIL_OFF_DURATION_SEC = 1.5


def _physical_read(device_path: str, offset: int, size: int) -> None:
    """Open the raw block device, pread one chunk, hint the kernel to drop
    the read pages from cache, close.

    POSIX_FADV_DONTNEED tells the kernel to evict the just-read pages so
    subsequent reads have to go back to the platter, keeping the LED
    firing on real disk I/O. Graceful no-op on any platform without
    posix_fadvise (dev Macs).
    """
    fd = os.open(device_path, os.O_RDONLY)
    try:
        os.pread(fd, size, offset)
        try:
            os.posix_fadvise(fd, offset, size, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
    finally:
        os.close(fd)


# -------------------- optional SES / IBPI fault LED -------------------- #

def _try_ledctl(action: str, device_path: str) -> bool:
    """Try to drive the bay's FAULT LED via `ledctl`.

    action: "fault" to light, "fault_off" to clear. Returns True if the
    command was found and exited 0 (chassis supports backplane LED
    management); False on any failure (command missing, exit non-zero,
    timeout, or OSError) so callers can treat it as a best-effort bonus.
    """
    ledctl = shutil.which("ledctl")
    if not ledctl:
        return False
    try:
        result = subprocess.run(
            [ledctl, f"{action}={device_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# -------------------- activity-LED patterns (always run) --------------- #

def _sustained_read_burst(
    device_path: str, offsets: list[int], size: int, duration_s: float
) -> int:
    """Issue back-to-back reads for duration_s seconds from a single fd.

    Runs in a thread (via asyncio.to_thread). The single-fd + in-thread
    loop eliminates per-read asyncio overhead — the drive's I/O queue
    stays continuously filled, which is what keeps the activity LED
    visibly lit on fast drives. On SSD, a 64 KB read completes in
    ~100 µs; the multi-millisecond thread hop that Python does per
    `asyncio.to_thread` call would otherwise dominate and make the
    LED flash instead of staying solid.

    Returns the number of reads done. Raises OSError if the device goes
    away mid-burst (drive pulled) — caller treats that as "exit blinker."
    """
    import time as _time  # local, so macOS dev path without posix_fadvise still imports
    fd = os.open(device_path, os.O_RDONLY)
    count = 0
    try:
        end = _time.monotonic() + duration_s
        n_offsets = len(offsets)
        while _time.monotonic() < end:
            offset = offsets[count % n_offsets]
            os.pread(fd, size, offset)
            try:
                os.posix_fadvise(fd, offset, size, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
            count += 1
    finally:
        os.close(fd)
    return count


# How long each burst runs in the worker thread. Bounds cancellation
# latency (cancel is honored at the next await boundary, i.e. once per
# burst) and controls the yield cadence for other asyncio tasks.
_BURST_DURATION_SEC = 0.5


async def _pass_solid_cycle(device_path: str, idx_ref: list[int]) -> bool:
    """Drive the activity LED to a visually-solid ON state forever.

    One sustained-read burst per cycle. The burst runs in a worker
    thread (bypassing per-read asyncio overhead), keeping the drive's
    I/O queue continuously filled so the activity LED stays lit.

    Returns False if the drive is gone (OSError).
    """
    try:
        count = await asyncio.to_thread(
            _sustained_read_burst, device_path, _PROBE_OFFSETS, _PROBE_SIZE, _BURST_DURATION_SEC
        )
        idx_ref[0] += count
    except OSError as exc:
        logger.info(
            "blinker exiting for %s: %s (drive likely pulled)",
            device_path, exc,
        )
        return False
    # Micro-yield so other asyncio tasks (dashboard polls, other blinkers)
    # don't get starved between bursts.
    await asyncio.sleep(0)
    return True


async def _lighthouse_cycle(device_path: str, idx_ref: list[int]) -> bool:
    """1.5 s sustained-read burst (LED solid-on) + 1.5 s dark.

    Same burst mechanism as the pass pattern, but with a clear 1.5 s
    dark gap afterward — which produces the distinctive ON-OFF
    lighthouse rhythm for failed drives.
    """
    try:
        count = await asyncio.to_thread(
            _sustained_read_burst, device_path, _PROBE_OFFSETS, _PROBE_SIZE, _FAIL_ON_DURATION_SEC
        )
        idx_ref[0] += count
    except OSError as exc:
        logger.info(
            "blinker exiting for %s: %s (drive likely pulled)",
            device_path, exc,
        )
        return False
    await asyncio.sleep(_FAIL_OFF_DURATION_SEC)
    return True


# ---------------------- top-level blinker task ------------------------ #

async def blink_done(device_path: str, *, pattern: Pattern = "pass") -> None:
    """Run the post-run LED pattern until the drive is pulled or cancelled.

    On fail-pattern drives, also tries `ledctl fault=<device>` once at
    start and `ledctl fault_off=<device>` on exit — no-op on chassis
    without backplane LED management (R720 LFF and anything else without
    SGPIO wired through), but a bonus amber LED on hardware that supports
    it. The read-pattern still runs either way so the signal isn't
    hardware-gated.
    """
    idx_ref = [0]
    cycle = _pass_solid_cycle if pattern == "pass" else _lighthouse_cycle
    ses_lit = False
    if pattern == "fail":
        ses_lit = await asyncio.to_thread(_try_ledctl, "fault", device_path)
        if ses_lit:
            logger.info("blinker: amber fault LED lit via ledctl for %s", device_path)
    try:
        while True:
            try:
                alive = await cycle(device_path, idx_ref)
                if not alive:
                    return
            except asyncio.CancelledError:
                return
    finally:
        if ses_lit:
            # Best-effort — don't block cancellation or raise.
            try:
                await asyncio.to_thread(_try_ledctl, "fault_off", device_path)
            except Exception:  # noqa: BLE001
                pass
