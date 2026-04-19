"""Hotplug monitor.

Wraps pyudev (Linux only). On macOS / other dev environments, exposes a
no-op monitor that never fires so the rest of the daemon can initialize
cleanly. Real event handling lives in the daemon orchestrator; this module
just translates kernel events into `HotplugEvent` records.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class EventKind(str, Enum):
    PRINTER_CONNECTED = "printer_connected"
    PRINTER_DISCONNECTED = "printer_disconnected"
    DRIVE_ADDED = "drive_added"
    DRIVE_REMOVED = "drive_removed"


@dataclass(frozen=True)
class HotplugEvent:
    kind: EventKind
    device_node: str | None
    vendor_id: str | None = None
    product_id: str | None = None
    model: str | None = None
    serial: str | None = None


BROTHER_VID = "04f9"


class Monitor:
    """Async iterator of hotplug events. No-op on non-Linux platforms."""

    def __init__(self) -> None:
        self._stopped = False
        self._context = None
        self._monitor = None
        try:
            import pyudev  # type: ignore[import-not-found]

            self._context = pyudev.Context()
            self._monitor = pyudev.Monitor.from_netlink(self._context)
            self._monitor.filter_by(subsystem="usb")
            self._monitor.filter_by(subsystem="block")
            self._monitor.start()
            logger.info("hotplug: pyudev monitor active")
        except Exception as exc:  # noqa: BLE001
            # pyudev missing, non-Linux, or libudev unavailable. Silent no-op.
            logger.info("hotplug: monitor disabled (%s)", exc)

    @property
    def enabled(self) -> bool:
        return self._monitor is not None

    async def events(self) -> AsyncIterator[HotplugEvent]:
        if not self.enabled:
            # Idle forever; caller should `asyncio.gather` with a cancel signal.
            import asyncio

            while not self._stopped:
                await asyncio.sleep(3600)
            return
        import asyncio

        loop = asyncio.get_event_loop()
        while not self._stopped:
            device = await loop.run_in_executor(None, self._monitor.poll, 1.0)  # type: ignore[union-attr]
            if device is None:
                continue
            kind = self._classify(device)
            if kind is None:
                continue
            yield HotplugEvent(
                kind=kind,
                device_node=device.device_node,
                vendor_id=device.properties.get("ID_VENDOR_ID"),
                product_id=device.properties.get("ID_MODEL_ID"),
                model=device.properties.get("ID_MODEL"),
                serial=device.properties.get("ID_SERIAL_SHORT"),
            )

    def _classify(self, device) -> EventKind | None:  # type: ignore[no-untyped-def]
        action = device.action
        subsystem = device.subsystem
        vid = (device.properties.get("ID_VENDOR_ID") or "").lower()
        if subsystem == "usb" and vid == BROTHER_VID:
            return EventKind.PRINTER_CONNECTED if action == "add" else EventKind.PRINTER_DISCONNECTED
        if subsystem == "block" and device.device_type == "disk":
            return EventKind.DRIVE_ADDED if action == "add" else EventKind.DRIVE_REMOVED
        return None

    def stop(self) -> None:
        self._stopped = True
