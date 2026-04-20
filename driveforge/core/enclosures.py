"""Enclosure / bay detection.

Three detection paths, tried in order:

1. **SES (SCSI Enclosure Services)** — preferred. Server-class SAS
   backplanes (R720 internal, MD1200, most storage shelves) expose a
   standard SES target that reports slot count, which slot holds which
   drive, fault/ident LEDs (settable via sg_ses/ledctl), and sometimes
   per-slot temp + power. DriveForge reads this via the kernel's
   `enclosure` sysfs class (`/sys/class/enclosure/<name>/<slot>/`) —
   no external dep, works without root.

2. **SAS layer (expander-only)** — fallback for hardware that has a
   SAS expander but no embedded SES processor. Some backplanes
   (including certain NX-3200 SKUs) advertise slot topology via SAS
   SMP — the kernel's `sas_device` class exposes `bay_identifier` +
   `enclosure_identifier` on each end_device — but don't expose an
   SES target. Without SES we can still show the real slot numbers,
   just not control LEDs (ledctl/ledmon require SES on SAS).

3. **Virtual bays** — final fallback for consumer PCs / NVMe-only
   rigs. The daemon uses `virtual_bays` from config, a user-chosen
   number of slots drives are assigned to in insertion order.

For LED control (set fault/ident), we fall back to `sg_ses` since kernel
sysfs is read-only for those attributes on most distros. On SAS-layer
enclosures (path 2), LED control is not available from userspace tools.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


SYS_ENCLOSURE_ROOT = Path("/sys/class/enclosure")
SYS_SAS_DEVICE_ROOT = Path("/sys/class/sas_device")


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


def _sas_end_device_to_block(sas_class_symlink: Path) -> str | None:
    """Walk from a `/sys/class/sas_device/end_device-X:Y:Z` symlink to the
    backing `/dev/sdX`, if any.

    The symlink resolves to `.../end_device-X:Y:Z/sas_device/end_device-X:Y:Z`
    (the INNER sas_device namespace dir). The block device lives two levels
    up at `.../end_device-X:Y:Z/target<H>:<C>:<T>/<H>:<C>:<T>:<L>/block/sd?`.
    """
    try:
        real = sas_class_symlink.resolve()
    except (OSError, RuntimeError):
        return None
    # The resolved path ends in `.../sas_device/end_device-X:Y:Z`. Go up two
    # levels to the actual end_device directory.
    end_device_dir = real.parent.parent
    if not end_device_dir.exists():
        return None
    # Any target directory under here holds the block device for a populated slot.
    # Empty slots have no target* subdir — _sas_end_device_to_block returns None.
    for target_dir in end_device_dir.glob("target*"):
        for lun_dir in target_dir.iterdir():
            if not lun_dir.is_dir():
                continue
            block_dir = lun_dir / "block"
            if not block_dir.exists():
                continue
            for blk in block_dir.iterdir():
                return f"/dev/{blk.name}"
    return None


def _find_expander_metadata(
    sas_device_root: Path, enclosure_identifier: str
) -> tuple[str, str]:
    """Given an enclosure_identifier (= an expander's sas_address), find
    that expander in `/sys/class/sas_device/` and return (vendor, model).

    Falls back to generic labels when vendor/model aren't in sysfs.
    """
    for entry in sas_device_root.iterdir():
        if not entry.name.startswith("expander-"):
            continue
        sas_addr = _read(entry / "sas_address") or ""
        if sas_addr != enclosure_identifier:
            continue
        try:
            real = entry.resolve()
        except (OSError, RuntimeError):
            continue
        # real = .../expander-0:0/sas_device/expander-0:0 → go up two, then device/
        expander_root = real.parent.parent
        vendor = _read(expander_root / "device" / "vendor") or "Unknown"
        model = _read(expander_root / "device" / "model") or "SAS expander"
        return vendor.strip(), model.strip()
    return "Unknown", "SAS expander (no SES)"


def discover_sas_enclosures(*, sys_root: Path = Path("/")) -> list[Enclosure]:
    """Discover enclosures via the kernel's SAS layer (no SES required).

    Used as a fallback when `/sys/class/enclosure/` is empty but the host
    has a SAS expander-based backplane that advertises slot topology via
    SMP. Produces Enclosure objects shaped the same as the SES path so
    the rest of the app doesn't need to distinguish.

    Caveats vs the SES path:
      - `sg_device` is None — no SES target to send page-02 writes to,
        so LED control (sg_ses / ledctl) is NOT available on these
        enclosures.
      - Slot count reflects only end_devices the expander has enumerated.
        Truly empty bays on the backplane may not be visible until a
        drive is inserted (unlike SES, which reports all physical slots).
      - Vendor/model come from the expander, not the chassis. Shows up
        in the UI as e.g. "LSI SAS2x36" instead of "Dell PERC H710
        backplane". Still useful for disambiguation; less human-friendly.
    """
    sas_device_root = sys_root / "sys" / "class" / "sas_device"
    if not sas_device_root.exists():
        return []

    # Bucket end_devices by their enclosure_identifier (= parent expander's
    # sas_address). Each bucket becomes one synthetic Enclosure.
    groups: dict[str, list[Slot]] = {}
    for entry in sorted(sas_device_root.iterdir()):
        if not entry.name.startswith("end_device-"):
            continue
        enc_id = _read(entry / "enclosure_identifier") or ""
        bay = _int(_read(entry / "bay_identifier"))
        if not enc_id or bay is None:
            # End device exists but isn't associated with an enclosure —
            # skip it (happens with direct-attach SAS with no expander).
            continue
        device_path = _sas_end_device_to_block(entry)
        slot = Slot(
            slot_number=bay,
            element_index=bay,  # no SES element index; reuse bay number
            populated=device_path is not None,
            device=device_path,
        )
        groups.setdefault(enc_id, []).append(slot)

    if not groups:
        return []

    enclosures: list[Enclosure] = []
    for enc_id, slots in groups.items():
        vendor, model = _find_expander_metadata(sas_device_root, enc_id)
        slots.sort(key=lambda s: s.slot_number)
        enclosures.append(
            Enclosure(
                sysfs_name=f"sas-{enc_id}",
                sg_device=None,  # no SES → no LED control
                vendor=vendor,
                product=model,
                logical_id=enc_id,
                slots=slots,
            )
        )
    enclosures.sort(key=lambda e: e.logical_id or "")
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

    Tries three detection paths in order:

      1. SES — preferred, full slot info + LED control.
      2. SAS layer — fallback for expander-only backplanes (no SES target).
         Gives real slot numbers but no LED control.
      3. Virtual bays — final fallback for consumer PCs / NVMe-only rigs.

    If SES enclosures are found, SAS-layer enclosures are deduplicated
    against them by `enclosure_identifier` / logical_id so we don't show
    the same backplane twice. In practice only one of the two paths ever
    has data on a given host, but the dedupe is defensive.

    Virtual bays are only added when NO real enclosures (SES or SAS) are
    found — mixing virtual + real is confusing and error-prone. Drives
    not in any real enclosure still show up on the dashboard; they go
    into the "Unbayed" bucket rather than a virtual slot.
    """
    ses_enclosures = discover_enclosures(sys_root=sys_root)
    sas_enclosures = discover_sas_enclosures(sys_root=sys_root)

    # Dedupe SAS results against SES results on logical_id (SES encloses
    # carry the expander/backplane SAS address too, typically).
    ses_ids = {e.logical_id for e in ses_enclosures if e.logical_id}
    sas_enclosures = [e for e in sas_enclosures if e.logical_id not in ses_ids]

    enclosures = ses_enclosures + sas_enclosures
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
