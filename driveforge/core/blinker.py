"""Drive activity-LED blinker.

Runs a distinctive read pattern against a finished drive so its bay's
activity LED blinks in a recognizable "pull me" rhythm. The R720 LFF
direct-attach backplane exposes no SES enclosure, so per-slot IDENT/FAULT
LEDs aren't reachable — but every drive's own activity LED is wired to
the bay, so we piggyback on that.

Two patterns, simple and visually distinct at a glance across the room:

- PASS (heartbeat): three short 64 KB pulses, 1.5 s dark. Reads as
  "chirp-chirp-chirp … pause". Fires for any successful run regardless
  of grade (A / B / C all look the same from across the room — the
  operator pulls them all the same way).
- FAIL (lighthouse): ~1.5 s of continuous reads to keep the LED visually
  lit, then 1.5 s of full dark. Reads as "solid ON … solid OFF, solid
  ON … solid OFF". Deliberately nothing like the chirp pattern or the
  natural in-flight activity of a drive under test.

The blinker stops as soon as the drive is pulled (open() raises OSError)
or when the task is cancelled (new batch, daemon shutdown, abort).

I/O volume is tiny: a few KB per cycle in PASS, a few hundred KB per
cycle in FAIL. We cycle through multiple offsets so the Linux block page
cache doesn't absorb repeated reads and silence the LED.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Literal

logger = logging.getLogger(__name__)

Pattern = Literal["pass", "fail"]

# Offsets to rotate through so repeated reads actually hit the device
# instead of being served from page cache OR the drive's own onboard DRAM
# cache (typically 16-64 MiB on modern HDDs). 500 offsets × 50 MiB stride
# spans ~25 GiB, well beyond any drive's internal cache, so even after
# kernel page-cache warms we're still hitting fresh platter for most reads.
_PROBE_OFFSETS = [i * (50 * 1024 * 1024) for i in range(500)]
# 64 KB per probe — a 4 KB read completes in ~5 ms on HDD which the human
# eye can miss entirely against the 1.5 s quiet gap in the heartbeat.
# 64 KB takes ~25-30 ms on spinning rust: visibly flashes AND generates
# enough cache pressure for POSIX_FADV_DONTNEED to actually get honored
# (advisory hints only trigger eviction when there's something to evict FOR).
_PROBE_SIZE = 64 * 1024

# FAIL lighthouse timing
_FAIL_ON_DURATION_SEC = 1.5
_FAIL_OFF_DURATION_SEC = 1.5
# Same 64 KB size as the heartbeat probe — each read long enough on
# rotational media that consecutive reads merge into a visibly "solid" LED.
_FAIL_READ_SIZE = 64 * 1024


def _physical_read(device_path: str, offset: int, size: int) -> None:
    """Open the raw block device, pread one small chunk, hint the kernel
    to drop the read pages from cache, close.

    The fadvise is *load-bearing*. Without it the Linux page cache absorbs
    our 20-offset rotation within ~1 GiB of span — after the first pass
    through the offsets, every subsequent read is served from memory, no
    block I/O actually hits the drive, and the activity LED goes dark.
    POSIX_FADV_DONTNEED asks the kernel to drop the just-read pages so
    the next cycle's reads have to go back to the platter, keeping the
    LED visibly pulsing indefinitely.

    Graceful no-op on macOS / any platform without posix_fadvise — the
    dashboard on dev laptops doesn't drive LEDs anyway.
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


async def _heartbeat_cycle(device_path: str, idx_ref: list[int]) -> bool:
    """Three 64 KB chirps + 1.5 s dark. Returns False if drive is gone."""
    for _ in range(3):
        offset = _PROBE_OFFSETS[idx_ref[0] % len(_PROBE_OFFSETS)]
        idx_ref[0] += 1
        try:
            await asyncio.to_thread(_physical_read, device_path, offset, _PROBE_SIZE)
        except OSError as exc:
            logger.info("blinker exiting for %s: %s (drive likely pulled)", device_path, exc)
            return False
        await asyncio.sleep(0.12)
    await asyncio.sleep(1.5)
    return True


async def _lighthouse_cycle(device_path: str, idx_ref: list[int]) -> bool:
    """Continuous reads for 1.5 s (LED solid-on) + 1.5 s dark. Returns
    False if drive is gone."""
    deadline = time.monotonic() + _FAIL_ON_DURATION_SEC
    while time.monotonic() < deadline:
        offset = _PROBE_OFFSETS[idx_ref[0] % len(_PROBE_OFFSETS)]
        idx_ref[0] += 1
        try:
            await asyncio.to_thread(_physical_read, device_path, offset, _FAIL_READ_SIZE)
        except OSError as exc:
            logger.info("blinker exiting for %s: %s (drive likely pulled)", device_path, exc)
            return False
    await asyncio.sleep(_FAIL_OFF_DURATION_SEC)
    return True


async def blink_done(device_path: str, *, pattern: Pattern = "pass") -> None:
    """Blink the activity LED until the drive is pulled or we're cancelled.

    Does not raise; logs and returns on any fatal I/O error (drive removed,
    permission lost, device renamed by hotplug).
    """
    idx_ref = [0]
    cycle = _heartbeat_cycle if pattern == "pass" else _lighthouse_cycle
    while True:
        try:
            alive = await cycle(device_path, idx_ref)
            if not alive:
                return
        except asyncio.CancelledError:
            return
