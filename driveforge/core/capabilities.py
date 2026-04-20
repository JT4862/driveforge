"""Hardware capability detection.

Answers the three yes/no questions the Setup Wizard and Settings surface
to the operator:

  - **led_control** — can we drive amber fault LEDs on this chassis?
    True iff at least one detected enclosure exposes an SES target
    (`sg_device` non-None). Without SES, userspace tools (`ledctl` /
    `sg_ses`) can't drive the backplane's LEDs, even when the LED
    hardware is physically present (see expander-only backplanes).

  - **chassis_power** — can we read instantaneous chassis power draw?
    True iff `ipmitool dcmi power reading` returns a parseable value.
    Needs `/dev/ipmi0` accessible to the daemon user (udev rule shipped
    with install.sh) + a BMC that implements IPMI DCMI.

  - **chassis_temperature** — can we read inlet/exhaust/CPU temps?
    True iff `ipmitool sdr` returns at least one "degrees C" line.
    Widely supported on server-class BMCs; absent on consumer PCs.

Cached on `DaemonState.capabilities`; refreshed on boot and on an
explicit "rescan" action. Slow-changing state — no automatic polling.
"""

from __future__ import annotations

from dataclasses import dataclass

from driveforge.core import enclosures as enclosures_mod
from driveforge.core.process import run


@dataclass
class HardwareCapabilities:
    led_control: bool
    chassis_power: bool
    chassis_temperature: bool

    @property
    def any_bmc_feature(self) -> bool:
        """Either chassis_power or chassis_temperature works → BMC is
        reachable at the hardware level. Useful for UI messaging."""
        return self.chassis_power or self.chassis_temperature


def detect(*, plan: enclosures_mod.BayPlan | None = None) -> HardwareCapabilities:
    """Probe all three capabilities. Safe on any host — missing hardware
    returns False cleanly with no side effects.

    If `plan` is provided, reuses it instead of running the sysfs scan
    again (the daemon typically passes `state.bay_plan`).
    """
    plan = plan if plan is not None else enclosures_mod.build_bay_plan()
    led = any(e.sg_device for e in plan.enclosures)

    pwr = run(["ipmitool", "dcmi", "power", "reading"])
    chassis_power = pwr.ok and "Instantaneous power" in pwr.stdout

    sdr = run(["ipmitool", "sdr"])
    chassis_temperature = sdr.ok and "degrees C" in sdr.stdout

    return HardwareCapabilities(
        led_control=led,
        chassis_power=chassis_power,
        chassis_temperature=chassis_temperature,
    )
