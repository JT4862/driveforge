"""v1.1.0+ server-hardware introspection for field-check mode.

When DriveForge boots from the field-check Live ISO at a seller's
house, the operator needs to know what they're potentially buying:
not just drive health, but server identity. CPU model, RAM, board
manufacturer, BMC presence, NIC count, PCIe inventory.

This module wraps the standard Linux introspection tools that are
already on the box (`dmidecode`, `lscpu`, `lspci`, `ip`, etc.)
and returns a flat dataclass that the field-check UI can render.

Design notes:

- All probes are best-effort. A missing tool, a permission denial,
  a Supermicro BMC that censors a SMBIOS field — all return None
  for that specific field rather than failing the whole report.
  An operator looking at "Memory: —" knows that field couldn't be
  read; they don't get a stack trace blocking the rest.
- All probes have explicit timeouts. A hung dmidecode shouldn't
  block the field-check page render forever.
- Output is JSON-serializable so the UI can render it directly
  AND the optional "Save report" button can dump it to the USB
  stick as machine-readable JSON.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Per-probe timeout. dmidecode + lspci are fast in practice (<1s);
# this cap defends against hung BMCs / firmware quirks.
_PROBE_TIMEOUT_S = 5.0


def _run(argv: list[str]) -> str | None:
    """Run a probe command, return stdout on success, None on any
    failure (timeout, missing binary, non-zero exit)."""
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except FileNotFoundError:
        logger.debug("server_info: %s not installed", argv[0])
        return None
    except subprocess.TimeoutExpired:
        logger.warning("server_info: %s timed out after %ds", argv[0], _PROBE_TIMEOUT_S)
        return None
    if r.returncode != 0:
        return None
    return r.stdout


@dataclass
class ServerInfo:
    """Flat, JSON-friendly snapshot of host hardware identity. Every
    field is Optional — None means "not captured" and the UI shows
    a dash for it."""

    # Identity
    manufacturer: str | None = None  # "Dell Inc.", "Supermicro", etc.
    product_name: str | None = None  # "PowerEdge R720", "X10DRi", etc.
    serial_number: str | None = None  # service tag / chassis serial
    bios_vendor: str | None = None
    bios_version: str | None = None
    bios_date: str | None = None

    # CPU
    cpu_model: str | None = None     # "Intel(R) Xeon(R) CPU E5-2680 v4"
    cpu_sockets: int | None = None
    cpu_cores_per_socket: int | None = None
    cpu_threads_total: int | None = None

    # Memory
    memory_total_gb: int | None = None
    memory_dimm_count: int | None = None
    memory_dimm_summary: str | None = None  # e.g. "8× 16GB DDR4-2400"

    # Network
    nic_count: int | None = None
    nic_summary: list[str] = field(default_factory=list)  # ["enp1s0 (1 Gb)", ...]

    # Storage controllers (SAS HBAs visible to lspci — not the drives themselves)
    storage_controllers: list[str] = field(default_factory=list)

    # BMC / IPMI presence (Supermicro / iDRAC / iLO)
    bmc_present: bool | None = None
    bmc_summary: str | None = None  # "iDRAC 8 detected", "Supermicro BMC detected", etc.


def collect() -> ServerInfo:
    """Run all probes + return a populated ServerInfo. Never raises;
    individual probe failures degrade to None on that field."""
    info = ServerInfo()
    _probe_dmi(info)
    _probe_cpu(info)
    _probe_memory(info)
    _probe_network(info)
    _probe_storage_controllers(info)
    _probe_bmc(info)
    return info


# ---------------------------------------------------------------- DMI


def _probe_dmi(info: ServerInfo) -> None:
    """Pull manufacturer / product / serial / BIOS via `dmidecode -t`
    queries. Each query is one-shot and short. dmidecode requires
    root; on the field-check Live ISO the daemon runs as root or
    has the right capabilities for /dev/mem access."""
    sys_out = _run(["dmidecode", "-t", "system"])
    if sys_out:
        info.manufacturer = _dmi_field(sys_out, "Manufacturer")
        info.product_name = _dmi_field(sys_out, "Product Name")
        info.serial_number = _dmi_field(sys_out, "Serial Number")
    bios_out = _run(["dmidecode", "-t", "bios"])
    if bios_out:
        info.bios_vendor = _dmi_field(bios_out, "Vendor")
        info.bios_version = _dmi_field(bios_out, "Version")
        info.bios_date = _dmi_field(bios_out, "Release Date")


def _dmi_field(text: str, key: str) -> str | None:
    """Extract `<key>: <value>` from dmidecode output. Returns None
    when the key is missing OR when the value is one of dmidecode's
    well-known noise placeholders ("Not Specified", "To be filled
    by O.E.M.", etc.)."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(key + ":"):
            value = line[len(key) + 1:].strip()
            if not value:
                return None
            # Filter out OEM-board placeholder garbage that dmidecode
            # surfaces when the vendor didn't bother filling SMBIOS.
            noise = {
                "not specified", "to be filled by o.e.m.", "default string",
                "system manufacturer", "system product name",
                "system serial number", "0", "n/a", "none",
            }
            if value.lower() in noise:
                return None
            return value
    return None


# ---------------------------------------------------------------- CPU


def _probe_cpu(info: ServerInfo) -> None:
    out = _run(["lscpu"])
    if not out:
        return
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = (part.strip() for part in line.split(":", 1))
        if k == "Model name":
            info.cpu_model = v
        elif k == "Socket(s)":
            try:
                info.cpu_sockets = int(v)
            except ValueError:
                pass
        elif k == "Core(s) per socket":
            try:
                info.cpu_cores_per_socket = int(v)
            except ValueError:
                pass
        elif k == "CPU(s)":
            try:
                info.cpu_threads_total = int(v)
            except ValueError:
                pass


# ---------------------------------------------------------------- Memory


def _probe_memory(info: ServerInfo) -> None:
    """`dmidecode -t memory` — DIMMs only (skip "Physical Memory Array"
    headers), summarize by size + speed pattern."""
    out = _run(["dmidecode", "-t", "memory"])
    if not out:
        return
    # Split into "Memory Device" blocks
    blocks = re.split(r"\n(?=Handle 0x[0-9A-Fa-f]+, DMI type 17,)", out)
    populated: list[tuple[int, str]] = []  # (size_gb, "DDR4-2400")
    for block in blocks:
        if "Memory Device" not in block:
            continue
        size = _dmi_field(block, "Size")
        if not size or "no module installed" in size.lower():
            continue
        # "16384 MB" → 16
        m = re.match(r"(\d+)\s*(MB|GB)", size, re.IGNORECASE)
        if not m:
            continue
        n, unit = int(m.group(1)), m.group(2).upper()
        size_gb = n if unit == "GB" else n // 1024
        speed = _dmi_field(block, "Configured Memory Speed") or _dmi_field(block, "Speed")
        type_ = _dmi_field(block, "Type")
        speed_summary = ""
        if type_:
            speed_summary = type_
            if speed:
                # Strip trailing "MT/s" / "MHz"
                m2 = re.match(r"(\d+)", speed)
                if m2:
                    speed_summary = f"{type_}-{m2.group(1)}"
        populated.append((size_gb, speed_summary))
    if not populated:
        return
    info.memory_dimm_count = len(populated)
    info.memory_total_gb = sum(s for s, _ in populated)
    # If all DIMMs are uniform: "8× 16GB DDR4-2400". Otherwise note mixed.
    sizes = {s for s, _ in populated}
    speeds = {sp for _, sp in populated if sp}
    if len(sizes) == 1 and len(speeds) <= 1:
        size_gb = next(iter(sizes))
        speed = next(iter(speeds), "")
        suffix = f" {speed}" if speed else ""
        info.memory_dimm_summary = f"{len(populated)}× {size_gb}GB{suffix}"
    else:
        info.memory_dimm_summary = (
            f"{len(populated)} DIMMs (mixed sizes/speeds), {info.memory_total_gb} GB total"
        )


# ---------------------------------------------------------------- Network


def _probe_network(info: ServerInfo) -> None:
    """`ip -o link show` — count physical NICs (skip lo, virtual,
    docker bridges, etc.)."""
    out = _run(["ip", "-o", "link", "show"])
    if not out:
        return
    nic_names: list[str] = []
    for line in out.splitlines():
        m = re.match(r"\d+:\s+([^:@]+)[:@]", line)
        if not m:
            continue
        name = m.group(1).strip()
        # Filter out lo, virtual interfaces, bridges
        if name == "lo" or name.startswith(("docker", "veth", "br-", "tun", "tap", "wg")):
            continue
        nic_names.append(name)
    info.nic_count = len(nic_names)
    info.nic_summary = nic_names


# ---------------------------------------------------------------- Storage controllers


def _probe_storage_controllers(info: ServerInfo) -> None:
    """`lspci -nn | grep -i 'sas\\|raid\\|sata controller\\|nvme'`
    — surfaces what's actually managing the disks. Useful for the
    "is this thing crossflashable?" buying question."""
    out = _run(["lspci"])
    if not out:
        return
    keywords = ("SAS", "RAID", "SATA controller", "Non-Volatile memory")
    controllers: list[str] = []
    for line in out.splitlines():
        if any(kw.lower() in line.lower() for kw in keywords):
            # Strip the leading "00:1f.2 " bus address; keep the description
            parts = line.split(" ", 1)
            controllers.append(parts[1].strip() if len(parts) > 1 else line)
    info.storage_controllers = controllers


# ---------------------------------------------------------------- BMC


def _probe_bmc(info: ServerInfo) -> None:
    """Detect iDRAC / iLO / Supermicro BMC presence via dmidecode's
    IPMI device entry. Doesn't try to log in — just notes
    presence + family for the buying decision."""
    out = _run(["dmidecode", "-t", "38"])  # IPMI Device Information
    if not out or "IPMI Device Information" not in out:
        info.bmc_present = False
        return
    info.bmc_present = True
    # Try to identify the family from the manufacturer / product fields
    # already collected.
    if info.manufacturer:
        m = info.manufacturer.lower()
        if "dell" in m:
            info.bmc_summary = "iDRAC (Dell)"
        elif "hewlett" in m or "hpe" in m or "hp inc" in m:
            info.bmc_summary = "iLO (HPE)"
        elif "supermicro" in m:
            info.bmc_summary = "Supermicro BMC"
        elif "lenovo" in m:
            info.bmc_summary = "XClarity (Lenovo)"
    if not info.bmc_summary:
        info.bmc_summary = "BMC present (vendor unknown)"
