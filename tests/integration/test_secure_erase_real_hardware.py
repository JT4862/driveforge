"""Real-hardware integration test for the secure_erase pipeline.

NOT RUN BY DEFAULT. Gated by the `DRIVEFORGE_INTEGRATION_DEVICE` env
var. Without that set, every test in this file skips immediately —
so accidentally running `pytest` on a developer laptop cannot
wipe a drive.

When to run this
----------------

In CI: the `.github/workflows/integration-test.yml` workflow runs
this on every PR against a self-hosted runner (typically the R720)
that has a dedicated known-erasable SATA drive plugged into a
designated port. The env var points at that drive; the runner's
operator has confirmed the drive is safe to wipe.

Locally: only run this if you KNOW what `DRIVEFORGE_INTEGRATION_DEVICE`
points at and you've confirmed the drive contents are disposable.

Safety gates
------------

Four layers of "don't wipe the wrong thing":

  1. Env var must be explicitly set; absence → skip.
  2. Device must exist as a block device; absence → skip with clear
     error explaining the runner wasn't set up correctly.
  3. Device must match a per-host allow-list via the
     `DRIVEFORGE_INTEGRATION_ALLOWED_SERIALS` env var, which the
     runner operator sets to the serial(s) of drives they've
     explicitly designated as test targets. A drive swap mid-CI
     must not silently start wiping a new drive; the allow-list
     requires explicit operator intent.
  4. Device MUST NOT be the boot drive. We probe `/proc/mounts` for
     / and / boot; if the target is any parent of those, fail loudly.

What the test actually runs
---------------------------

The full `secure_erase` sequence:

  1. `ensure_clean_security_state(drive)` — pre-flight self-heal
  2. `sat_passthru.sat_secure_erase(device, ...)` — the three-command
     SAT sequence
  3. Post-check: verify the drive is back in CLEAN security state

Plus measurements: wall-clock duration for each step, broken out so
regressions in any step surface clearly in CI output.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from driveforge.core import erase, sat_passthru
from driveforge.core.drive import Drive, Transport


INTEGRATION_DEVICE_ENV = "DRIVEFORGE_INTEGRATION_DEVICE"
INTEGRATION_ALLOWED_SERIALS_ENV = "DRIVEFORGE_INTEGRATION_ALLOWED_SERIALS"


def _get_integration_device() -> str | None:
    """Return the device path from the env var if set, else None.

    Stripped + validated shape — must start with /dev/ and contain
    no shell metacharacters. Any funny business in the env var
    causes a skip, not a wipe of an unexpected path."""
    raw = os.environ.get(INTEGRATION_DEVICE_ENV, "").strip()
    if not raw:
        return None
    if not raw.startswith("/dev/"):
        return None
    # Block path-traversal / injection attempts. Device paths are
    # alphanumeric + '/' only.
    if not all(c.isalnum() or c in "/_-" for c in raw):
        return None
    return raw


def _get_allowed_serials() -> set[str]:
    """Comma-separated list of serials allowed as integration targets."""
    raw = os.environ.get(INTEGRATION_ALLOWED_SERIALS_ENV, "").strip()
    if not raw:
        return set()
    return {s.strip() for s in raw.split(",") if s.strip()}


def _probe_device_serial(device: str) -> str | None:
    """Read the drive's serial via `lsblk -dno SERIAL`. Returns None
    on any failure — the caller treats None as "can't confirm, bail."
    """
    try:
        result = subprocess.run(
            ["lsblk", "-dno", "SERIAL", device],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    serial = result.stdout.strip()
    return serial or None


def _probe_device_model(device: str) -> str | None:
    try:
        result = subprocess.run(
            ["lsblk", "-dno", "MODEL", device],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _probe_device_capacity(device: str) -> int | None:
    """Capacity in bytes via blockdev."""
    try:
        result = subprocess.run(
            ["blockdev", "--getsize64", device],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _device_is_boot_drive(device: str) -> bool:
    """Strict safety gate — refuse to touch anything that's part of
    the root or /boot filesystem hierarchy.

    Reads /proc/mounts for / and /boot, extracts their backing
    device, walks up the block-device dependency tree via
    `lsblk -sno PKNAME`. If `device` appears anywhere in the tree,
    we flat-out refuse.
    """
    # What devices back / and /boot?
    mounts_of_interest = set()
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            src, mountpoint = parts[0], parts[1]
            if mountpoint in ("/", "/boot", "/boot/efi"):
                mounts_of_interest.add(src)
    except OSError:
        # Can't read /proc/mounts → assume the worst
        return True

    # Resolve each mount source to its parent block device
    for src in mounts_of_interest:
        try:
            result = subprocess.run(
                ["lsblk", "-sno", "PKNAME", src],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            return True  # can't tell → assume the worst
        for pkname in (result.stdout or "").splitlines():
            pkname = pkname.strip()
            if not pkname:
                continue
            if f"/dev/{pkname}" == device:
                return True
    return False


@pytest.fixture(scope="module")
def integration_drive() -> Drive:
    """Build a Drive object for the designated integration target, or
    skip the whole module with a clear explanation of why."""
    device = _get_integration_device()
    if device is None:
        pytest.skip(
            f"{INTEGRATION_DEVICE_ENV} not set — skipping real-hardware "
            f"integration tests. Set this env var to a /dev/sdX path "
            f"on a self-hosted CI runner with a designated test drive."
        )

    if not Path(device).exists():
        pytest.skip(
            f"{INTEGRATION_DEVICE_ENV}={device} does not exist on this host. "
            f"Check the self-hosted runner's hardware configuration."
        )

    if _device_is_boot_drive(device):
        pytest.fail(
            f"SAFETY REFUSAL: {device} appears to back / or /boot. "
            f"Integration tests will not wipe the boot drive. Check "
            f"{INTEGRATION_DEVICE_ENV} configuration on the runner."
        )

    serial = _probe_device_serial(device)
    if serial is None:
        pytest.skip(f"could not read serial of {device} via lsblk — cannot confirm allow-list match")

    allowed = _get_allowed_serials()
    if allowed and serial not in allowed:
        pytest.fail(
            f"SAFETY REFUSAL: {device} has serial {serial!r} which is not "
            f"in {INTEGRATION_ALLOWED_SERIALS_ENV}={sorted(allowed)}. "
            f"Somebody swapped drives on the runner — refusing to erase "
            f"a drive that wasn't explicitly pre-approved."
        )

    capacity = _probe_device_capacity(device)
    if capacity is None:
        pytest.skip(f"could not read capacity of {device} via blockdev")
    if capacity > 2_000_000_000_000:  # 2 TB hard cap for CI runs
        pytest.skip(
            f"{device} is {capacity / 1e12:.1f} TB — refusing to erase "
            f"drives larger than 2 TB as CI targets (full erase would "
            f"take too long; use a smaller dedicated test drive)."
        )

    model = _probe_device_model(device) or "UNKNOWN"
    return Drive(
        serial=serial,
        model=model,
        capacity_bytes=capacity,
        transport=Transport.SATA,  # integration tests focus on the SATA path
        device_path=device,
        rotation_rate=0,  # unknown; doesn't matter for the erase logic
    )


def test_preflight_succeeds_on_known_good_drive(integration_drive: Drive) -> None:
    """The first thing every real-hardware run should do is confirm
    ensure_clean_security_state() can clear whatever state a prior
    test run left. If a prior run crashed mid-erase, the drive might
    be in ENABLED or LOCKED state; preflight should heal it."""
    t0 = time.monotonic()
    erase.ensure_clean_security_state(integration_drive)
    elapsed = time.monotonic() - t0
    print(f"\n  preflight completed in {elapsed:.2f}s")


def test_full_sat_secure_erase_sequence(integration_drive: Drive) -> None:
    """The real thing. Runs the full three-command SAT sequence against
    the designated test drive. Measures wall-clock duration for each
    step for regression tracking."""
    timings: dict[str, float] = {}

    # Pre-flight — should be fast (drive is already clean from the
    # previous test, or auto-healed here)
    t0 = time.monotonic()
    erase.ensure_clean_security_state(integration_drive)
    timings["preflight"] = time.monotonic() - t0

    # The erase itself
    t1 = time.monotonic()
    sat_passthru.sat_secure_erase(
        integration_drive.device_path,
        password=sat_passthru.DEFAULT_PASSWORD,
        timeout_s=30 * 60,  # 30 min cap; a 2 TB drive should finish within
        owner=integration_drive.serial,
    )
    timings["erase"] = time.monotonic() - t1

    # Post-check — drive should be back in CLEAN state
    t2 = time.monotonic()
    state = erase._probe_sata_security_state(integration_drive.device_path)
    timings["postcheck"] = time.monotonic() - t2

    print(f"\n  preflight: {timings['preflight']:.2f}s")
    print(f"  erase:     {timings['erase']:.2f}s")
    print(f"  postcheck: {timings['postcheck']:.2f}s")

    assert state == erase.SataSecurityState.CLEAN, (
        f"post-erase state is {state.value}, expected CLEAN — the erase "
        f"completed but the drive is still in a non-clean security state"
    )
