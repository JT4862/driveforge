"""Drive activity-LED blinker.

Runs a distinctive read pattern against a finished drive so its bay's
activity LED blinks in a recognizable "pull me" rhythm. The R720 LFF
direct-attach backplane exposes no SES enclosure, so per-slot IDENT/FAULT
LEDs aren't reachable — but every drive's own activity LED is wired to
the bay, so we piggyback on that.

Two patterns, deliberately chosen to look different to a human eye one
rack away — the previous FAIL pattern (one read every 0.6 s) read as
"solid on" next to a running drive, which defeats the point:
- PASS: three short 4 KB pulses, 1.5 s dark — fast chirping heartbeat.
- FAIL: two 256 KB pulses close together, 3 s dark — slow double-tap
  with a distinctly long dark gap between bursts.

The blinker stops as soon as the drive is pulled (open() raises OSError)
or when the task is cancelled (new batch, daemon shutdown, abort).

I/O volume is negligible: ~12 KB every ~2 seconds in PASS mode. We cycle
through multiple offsets so the Linux block page cache doesn't absorb
reads and silence the LED.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

Pattern = Literal["pass", "fail"]

# Offsets to rotate through so repeated reads actually hit the device
# instead of being served from page cache. Stays inside the first ~1 GiB
# of every drive we'd plausibly see.
_PROBE_OFFSETS = [i * (50 * 1024 * 1024) for i in range(20)]
_PROBE_SIZE = 4096


def _physical_read(device_path: str, offset: int, size: int) -> None:
    """Open the raw block device, pread one small chunk, close."""
    fd = os.open(device_path, os.O_RDONLY)
    try:
        os.pread(fd, size, offset)
    finally:
        os.close(fd)


async def blink_done(device_path: str, *, pattern: Pattern = "pass") -> None:
    """Blink the activity LED until the drive is pulled or we're cancelled.

    Does not raise; logs and returns on any fatal I/O error (drive removed,
    permission lost, device renamed by hotplug).
    """
    idx = 0
    if pattern == "pass":
        pulses = 3
        pulse_delay = 0.12
        tail_pause = 1.5
        read_size = _PROBE_SIZE
    else:
        # Double-tap + long silence. The 3 s dark pause is the key part:
        # it lets the LED fully decay so the pattern reads visibly
        # different from a running drive or a PASS heartbeat.
        pulses = 2
        pulse_delay = 0.25
        tail_pause = 3.0
        read_size = _PROBE_SIZE * 64  # 256 KB, longer visible pulse
    while True:
        try:
            for _ in range(pulses):
                offset = _PROBE_OFFSETS[idx % len(_PROBE_OFFSETS)]
                idx += 1
                try:
                    await asyncio.to_thread(_physical_read, device_path, offset, read_size)
                except OSError as exc:
                    logger.info(
                        "blinker exiting for %s: %s (drive likely pulled)",
                        device_path, exc,
                    )
                    return
                if pulse_delay:
                    await asyncio.sleep(pulse_delay)
            await asyncio.sleep(tail_pause)
        except asyncio.CancelledError:
            return
