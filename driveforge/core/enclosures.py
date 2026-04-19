"""Enclosure / bay detection via SES (SCSI Enclosure Services).

Server-class SAS backplanes (R720 internal, MD1200, most storage shelves)
expose a standard SES interface that reports:
  - Number of physical slots
  - Which slot holds which drive
  - Fault / identify LEDs (settable)
  - Temperature and power readings per slot (sometimes)

DriveForge reads this via the Linux kernel's `enclosure` sysfs class, which
exposes a clean per-slot view at `/sys/class/enclosure/<name>/<slot>/`. The
sysfs path was chosen over shelling out to `sg_ses` because:
  - No external dep
  - Works without root
  - Consistent across kernels back to ~3.x

For LED control (set fault/ident), we fall back to `sg_ses` since kernel
sysfs is read-only for those attributes on most distros.

Fallback: if no SES enclosures exist (consumer PC, NVMe-only rig), the
daemon uses `virtual_bays` from config — a user-chosen number of slots
that drives are assigned to in insertion order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


SYS_ENCLOSURE_ROOT = Path("/sys/class/enclosure")


@dataclass
class Slot:
    """One slot inside an enclosure."""

    slot_number: int  # 0-indexed in sysfs, displayed as 1-indexed in UI
    element_index: int  # SES element index (used for LED control)
    populated: bool
    device: str | None  # `/dev/sdX` when populated, else None
    vendor: str | None = None
    product: str | None = None
    serial: str | None = None


@dataclass
class Enclosure:
    """A physical drive enclosure — typically the server backplane or a JBOD."""

    sysfs_name: str  # e.g. "0:0:32:0"
    sg_device: str | None  # e.g. "/dev/sg3"; needed for sg_ses LED control
    vendor: str = "Unknown"
    product: str = "Enclosure"
    logical_id: str | None = None  # world-wide ID
    slots: list[Slot] = field(default_factory=list)

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    @property
    def populated_count(self) -> int:
        return sum(1 for s in self.slots if s.populated)

    @property
    def label(self) -> str:
        if self.product and self.product != "Enclosure":
            return self.product
        return f"{self.vendor} enclosure"


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _int(s: str | None) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _resolve_block_device(device_symlink: Path) -> str | None:
    """Given a sysfs `device` symlink, return the corresponding /dev/sdX path."""
    if not device_symlink.exists():
        return None
    # Either the symlink points to a scsi_device, and we walk down to find the
    # block device, or it directly has a `block` subdir in newer kernels.
    try:
        target = device_symlink.resolve()
    except (OSError, RuntimeError):
        return None
    block_dir = target / "block"
    if not block_dir.exists():
        # Older kernels: block/sdX is under the scsi_device directory
        return None
    for entry in block_dir.iterdir():
        return f"/dev/{entry.name}"
    return None


def _discover_enclosure(enc_root: Path, sys_block_base: Path) -> Enclosure | None:
    """Parse one /sys/class/enclosure/<name>/ directory."""
    sysfs_name = enc_root.name
    vendor = _read(enc_root / "device" / "vendor") or "Unknown"
    product = _read(enc_root / "device" / "model") or "Enclosure"
    logical_id = _read(enc_root / "id")

    # Find the sg device backing this enclosure (needed for sg_ses LED control)
    sg_device = None
    scsi_generic_dir = enc_root / "device" / "scsi_generic"
    if scsi_generic_dir.exists():
        for entry in scsi_generic_dir.iterdir():
            sg_device = f"/dev/{entry.name}"
            break

    slots: list[Slot] = []
    # Slots are named by their SES element descriptor. Kernel numbers them
    # by element_index order but the symlink name can vary (SLOT_0, SLOT 1,
    # Slot 01, etc.). Enumerate every directory that has a `slot` attribute.
    for entry in sorted(enc_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name == "device" or entry.name == "power" or entry.name.startswith("subsystem"):
            continue
        slot_num = _int(_read(entry / "slot"))
        if slot_num is None:
            continue
        status = _read(entry / "status") or "Unknown"
        element_index = _int(_read(entry / "type")) or slot_num
        populated = status.lower() in {"ok", "populated", "unknown"}  # "Unknown" is kernel default when device seen but not classified
        # The "device" entry is a symlink to the backing SCSI device
        device_path = _resolve_block_device(entry / "device")
        # If the symlink failed but status says populated, we'll still mark
        # the slot populated and just not have a device path. That happens
        # e.g. mid-udev-propagation.
        populated = populated or device_path is not None
        slots.append(
            Slot(
                slot_number=slot_num,
                element_index=element_index,
                populated=populated and (device_path is not None or status.lower() == "ok"),
                device=device_path,
            )
        )

    if not slots:
        logger.info("enclosure %s reported no slots; skipping", sysfs_name)
        return None

    # Stable slot ordering by slot number
    slots.sort(key=lambda s: s.slot_number)

    return Enclosure(
        sysfs_name=sysfs_name,
        sg_device=sg_device,
        vendor=vendor,
        product=product,
        logical_id=logical_id,
        slots=slots,
    )


def discover_enclosures(*, sys_root: Path = Path("/")) -> list[Enclosure]:
    """Discover all SES enclosures attached to this host.

    `sys_root` lets tests point at a synthetic sysfs tree.
    """
    enc_root = sys_root / "sys" / "class" / "enclosure"
    if not enc_root.exists():
        return []
    sys_block_base = sys_root / "sys" / "block"
    enclosures: list[Enclosure] = []
    for entry in sorted(enc_root.iterdir()):
        if not entry.is_dir():
            continue
        enc = _discover_enclosure(entry, sys_block_base)
        if enc is not None:
            enclosures.append(enc)
    return enclosures


@dataclass
class BayPlan:
    """Computed bay map used by the dashboard + orchestrator.

    Bays are assigned a unique `global_bay` id across the entire rig.
    Real slots are grouped by their enclosure for display; virtual bays
    (for hosts without SES) sit in their own group.
    """

    enclosures: list[Enclosure]
    virtual_bay_count: int  # 0 if any real enclosures were found
    total_bays: int  # sum of real slots + virtual bays

    @property
    def has_real_enclosures(self) -> bool:
        return bool(self.enclosures)


def build_bay_plan(*, sys_root: Path = Path("/"), virtual_bays_fallback: int = 8) -> BayPlan:
    """Compute the bay plan for this host.

    If real SES enclosures are found, virtual bays are NOT added (mixing
    virtual + real is confusing and error-prone). Drives not in any real
    enclosure still show up on the dashboard — they just go into the
    "Unbayed" bucket rather than a virtual slot.
    """
    enclosures = discover_enclosures(sys_root=sys_root)
    if enclosures:
        total = sum(e.slot_count for e in enclosures)
        return BayPlan(enclosures=enclosures, virtual_bay_count=0, total_bays=total)
    n = max(0, int(virtual_bays_fallback))
    return BayPlan(enclosures=[], virtual_bay_count=n, total_bays=n)


def bay_key_for_device(plan: BayPlan, device_path: str) -> str | None:
    """Find the bay_key for a given `/dev/sdX` if it's in an enclosure slot."""
    for enc_idx, enc in enumerate(plan.enclosures):
        for slot in enc.slots:
            if slot.device == device_path:
                return f"e{enc_idx}:s{slot.slot_number}"
    return None


def assign_virtual_bay(plan: BayPlan, used_keys: set[str]) -> str | None:
    """Pick the next unused virtual bay key. Returns None if all are used."""
    for i in range(plan.virtual_bay_count):
        key = f"v{i}"
        if key not in used_keys:
            return key
    return None


def unbayed_key(serial: str) -> str:
    return f"u:{serial}"
