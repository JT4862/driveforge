---
title: Identify LED
---

# Identify LED

*Available since v0.2.9.*

You're standing at the rack with 14 nearly-identical drives, and the
dashboard shows you `Z1F248SL` is the one you want to pull. Which
physical bay is it in?

Click the **Ident** button on the drive's card. The drive's LED
starts strobing rapidly. Walk to the rack, find the blinking drive,
pull it. Click **Stop** when done (or let the 5-minute auto-stop
fire).

## Where the button lives

Bottom-right corner of every **Installed**-section card. Active
drives don't have an Ident button — the pipeline is already lighting
their activity LED via real I/O, so identify would add no signal.

The button is two-state:

- **Ident** (blue tint) — no identify running. Click to start.
- **Stop** (warm amber, gently pulsing) — identify is currently
  active. Click to stop and restore the prior LED pattern.

## What actually blinks

Two layers, both attempted:

### 1. Blue locate LED (chassis-dependent)

On chassis with proper backplane management — SES enclosure with
SGPIO/IBPI wiring through to per-bay LEDs — DriveForge runs:

```
ledctl locate=/dev/sdX
```

That lights the bay's blue **locate** LED (the same indicator that
Dell iDRAC, HP iLO, and Lenovo XClarity flip when you click "blink
to identify"). Bright, steady-on (not blinking) on most chassis,
blinking on some. Distinguishable from any other LED state.

When the strobe stops:

```
ledctl locate_off=/dev/sdX
```

Chassis without SGPIO wired through (R720 LFF backplane, NX-3200
expander-only backplane) silently no-op this layer — `ledctl`
returns non-zero, we move on. No error, no warning.

### 2. Activity LED rapid strobe (universal fallback)

Always runs. DriveForge issues 64 KB reads to the drive in a tight
loop:

- 120 ms of back-to-back reads (LED visibly active)
- 120 ms of silence (LED off)
- Repeat

That's distinctly faster than any natural pipeline phase or the
post-run heartbeat / lighthouse patterns. A rack walker can pick the
strobing drive out at a glance even among other blinking drives.

The reads target offsets across a 25 GiB sweep so the kernel page
cache and the drive's onboard DRAM cache don't serve them — every
read hits the platter / NAND, keeping the LED genuinely lit.

## Auto-stop

A safety deadline of **5 minutes** stops the strobe automatically,
matching iDRAC and iLO defaults. If you forget to click Stop, the
LED returns to its prior state on its own; the drive isn't
churning I/O all night.

The 5-minute cap is hardcoded as `IDENTIFY_MAX_DURATION_SEC` in
`driveforge/core/blinker.py`. If your use case wants a different cap,
that's the line to change.

## Restoration of prior LED pattern

When identify exits (deadline, Stop click, or drive pulled),
DriveForge calls `restore_blinker_for_drive` — which re-spawns the
drive's pass/fail post-run blinker (heartbeat for pass, lighthouse
for fail). Net result: clicking Ident + Stop on a Grade A drive
returns the LED to the heartbeat pattern it was showing before, with
no operator intervention needed.

Drives with no completed run (never tested, aborted, etc.) have no
post-run blinker, so they go dark after Stop.

## What happens if the drive is pulled mid-strobe

The 64 KB read loop hits an `OSError` when the device file
disappears. The blinker exits cleanly, calls `ledctl locate_off=` if
it had lit the SES LED, and removes itself from
`state.identify_blinkers`. The next dashboard refresh shows the
drive gone from the Installed section.

## Refusal: the button isn't on Active cards

By design. An active drive is already lighting its activity LED via
real test I/O — the strobe would either (a) add nothing visible if
the pipeline phase generates lots of I/O (badblocks), or (b) fight
with real test I/O for the same block device (secure erase doesn't,
but identify's reads would block on the drive's busy state and
could hang). Cleaner to just hide the button.

If you really need to find an active drive in the rack, the
phase-colored progress bar + drive-temperature label on the card
should narrow it down — then visually verify by drive serial against
the chassis's drive-bay labels (if any).

## Hardware behavior matrix

| Chassis | SES locate LED | Activity strobe |
|---------|----------------|------------------|
| R720 LFF (3.5" backplane) | ✗ no SES | ✓ |
| R720 SFF (2.5" backplane) | ✓ via 9207-8i | ✓ |
| NX-3200 (expander-only) | ✗ no SES target | ✓ |
| Dell MD1200 (JBOD) | ✓ | ✓ |
| Generic SES-capable backplane | ✓ | ✓ |
| Direct motherboard SATA | ✗ no enclosure mgmt | ✓ |

The activity strobe is the universal signal — works on every drive
regardless of backplane. The blue locate LED is bonus signal where
hardware supports it.
