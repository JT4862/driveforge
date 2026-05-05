"""v1.1.2+ — terminal-rendered field-check report.

Companion to the web UI at `http://<host>:8080/`. Same data, different
surface. Designed for the air-gapped seller's-house workflow where the
operator has the server's own keyboard + monitor but no network path
to a laptop.

The Field-Check Live ISO autologs in on tty1 and runs this script via
`/root/.bash_profile` after boot, so the operator's first interaction
is already a populated report. They can rerun anytime by typing
`driveforge-field-report` at the shell.

Output is plain ANSI — no `rich`, no `curses`, no special terminal
mode. Works on serial console, VGA, SSH, anywhere `print()` does.

Reuses the same drive discovery + SMART probe + grading rules + server-
info collector that feed the web UI, so the report content is
guaranteed identical to what `GET /` would return.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

from driveforge import __version__
from driveforge.core import drive as drive_mod
from driveforge.core import grading, server_info
from driveforge.core import smart as smart_mod
from driveforge.core.drive_class import classify

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- ANSI codes


# Minimal palette. Avoid setting bg colors so the output reads OK on
# both light + dark terminals.
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_ORANGE = "\033[33;1m"  # bold yellow reads as orange on most terminals
_RED = "\033[31m"
_CYAN = "\033[36m"
_BLUE = "\033[34m"


def _color_for_grade(grade: str | None) -> str:
    """Map grade letters to terminal color codes. None / unknown → dim."""
    if grade is None:
        return _DIM
    g = grade.upper()
    if g == "A":
        return _GREEN
    if g == "B":
        return _CYAN
    if g == "C":
        return _ORANGE
    if g in ("F", "FAIL"):
        return _RED
    return _DIM


# ---------------------------------------------------------------- Rendering


def _hr(width: int = 76, char: str = "═") -> str:
    return char * width


def _render_header() -> str:
    """Banner + version + timestamp."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    title = f"DriveForge Field-Check  ·  v{__version__}  ·  {now}"
    return f"{_BOLD}{_BLUE}{_hr()}{_RESET}\n  {_BOLD}{title}{_RESET}\n{_BOLD}{_BLUE}{_hr()}{_RESET}"


def _render_server(info: server_info.ServerInfo) -> str:
    """Server identity panel."""
    rows: list[tuple[str, str]] = []
    if info.manufacturer or info.product_name:
        product = " ".join(p for p in (info.manufacturer, info.product_name) if p)
        rows.append(("Server", product))
    if info.serial_number:
        rows.append(("Serial", info.serial_number))
    if info.bios_vendor or info.bios_version:
        bios = " ".join(p for p in (info.bios_vendor, info.bios_version) if p)
        if info.bios_date:
            bios += f" ({info.bios_date})"
        rows.append(("BIOS", bios))
    if info.cpu_model:
        cpu = info.cpu_model
        if info.cpu_sockets and info.cpu_cores_per_socket:
            cpu += (
                f" · {info.cpu_sockets}× sockets, "
                f"{info.cpu_cores_per_socket} cores each, "
                f"{info.cpu_threads_total} threads total"
            )
        rows.append(("CPU", cpu))
    if info.memory_total_gb:
        mem = f"{info.memory_total_gb} GB"
        if info.memory_dimm_summary:
            mem += f" · {info.memory_dimm_summary}"
        rows.append(("Memory", mem))
    if info.nic_count:
        nics = f"{info.nic_count} × " + ", ".join(info.nic_summary[:6])
        if len(info.nic_summary) > 6:
            nics += f" (+{len(info.nic_summary) - 6} more)"
        rows.append(("NICs", nics))
    if info.bmc_present:
        rows.append(("BMC", info.bmc_summary or "Present"))

    if not rows:
        return f"  {_DIM}(server-info collection returned no data — running as non-root?){_RESET}"

    lines = [f"  {_BOLD}Server{_RESET}"]
    for label, value in rows:
        lines.append(f"  {_DIM}{label:<8}{_RESET}  {value}")
    return "\n".join(lines)


def _render_raid_warning(info: server_info.ServerInfo) -> str | None:
    """If the host has a RAID controller (not in IT/HBA mode), surface
    a clear warning explaining what it means for drive inspection.
    Returns None when no RAID controllers are present (clean case)."""
    raid = server_info.detect_raid_situation(info)
    if not raid["has_raid_controller"]:
        return None
    lines = [
        "",
        f"  {_BOLD}{_ORANGE}⚠  RAID controller detected — physical drives may not be directly visible{_RESET}",
        "",
    ]
    for ctrl in raid["raid_controllers"]:
        lines.append(f"     {_DIM}Controller:{_RESET} {ctrl}")
    if raid["has_passthrough_hba"]:
        lines.append(
            f"     {_DIM}Note:{_RESET} A passthrough HBA is also present — "
            f"drives behind that HBA WILL show real SMART data."
        )
    lines.extend([
        "",
        f"     {_DIM}What this means for the buying decision:{_RESET}",
        f"       • SMART health of underlying drives behind the RAID card: {_BOLD}UNKNOWN{_RESET}",
        f"       • Drive count behind the controller: not visible without the RAID config",
        f"       • To inspect physical drives:",
        f"         (a) Reboot into the RAID setup (typically Ctrl-R at POST) and",
        f"             check if the controller has an HBA / IT-mode toggle",
        f"         (b) PERC H310/H710 + LSI 9261/9271 are usually crossflashable to",
        f"             LSI 9211/9207 IT firmware (~30 min, well-documented)",
        f"         (c) Pull the drives + inspect on a known-IT-mode HBA",
    ])
    return "\n".join(lines)


def _render_drives(drives: list[dict]) -> str:
    """Drive table with grade in color. Pulls live SMART + grades each
    one. Slow-ish (smartctl per drive); the operator waits ~1-3 sec
    per drive while this runs."""
    if not drives:
        return (
            f"  {_BOLD}Drives (0){_RESET}\n"
            f"  {_DIM}No drives detected. Either nothing is plugged in, or the host's HBA{_RESET}\n"
            f"  {_DIM}isn't passing drives through (check the RAID warning above).{_RESET}"
        )

    real_count = sum(1 for d in drives if not d["is_virtual_disk"])
    virtual_count = len(drives) - real_count
    header = f"  {_BOLD}Drives ({real_count} real"
    if virtual_count:
        header += f", {virtual_count} virtual / RAID volume"
    header += f"){_RESET}"

    # Column widths sized for typical drive identifiers; serials over
    # 14 chars get truncated with an ellipsis.
    col_idx = 3
    col_serial = 14
    col_model = 22
    col_size = 8
    col_type = 5
    rows = [header, ""]
    rows.append(
        f"  {_DIM}"
        f"{'#':<{col_idx}} "
        f"{'Serial':<{col_serial}} "
        f"{'Model':<{col_model}} "
        f"{'Size':<{col_size}} "
        f"{'Type':<{col_type}} "
        f"Health{_RESET}"
    )
    rows.append(
        f"  {_DIM}"
        f"{'-' * col_idx} "
        f"{'-' * col_serial} "
        f"{'-' * col_model} "
        f"{'-' * col_size} "
        f"{'-' * col_type} "
        f"{'------'}{_RESET}"
    )

    for i, d in enumerate(drives, start=1):
        serial = d["serial"][:col_serial]
        model = (d["model"] or "Unknown")[:col_model]
        size = f"{d['capacity_tb']:.1f} TB" if d["capacity_tb"] else "—"
        type_ = d["type"]
        if d["is_virtual_disk"]:
            health = f"{_DIM}— (RAID volume){_RESET}"
        else:
            color = _color_for_grade(d.get("grade"))
            grade_glyph = (d.get("grade") or "?").upper()[0]
            ceiling = d.get("ceiling_reason")
            if d.get("error"):
                health = f"{_RED}ERR{_RESET}  {_DIM}{d['error'][:40]}{_RESET}"
            elif ceiling:
                health = f"{color}{grade_glyph}{_RESET}    {_DIM}{ceiling[:48]}{_RESET}"
            else:
                health = f"{color}{grade_glyph}{_RESET}"
        rows.append(
            f"  "
            f"{i:<{col_idx}} "
            f"{serial:<{col_serial}} "
            f"{model:<{col_model}} "
            f"{size:<{col_size}} "
            f"{type_:<{col_type}} "
            f"{health}"
        )
    return "\n".join(rows)


def _render_footer(daemon_url: str | None) -> str:
    lines = ["", f"  {_BOLD}Network{_RESET}"]
    if daemon_url:
        lines.append(f"  {_DIM}Web UI:{_RESET}  {daemon_url}")
    else:
        lines.append(
            f"  {_DIM}No network — running fully air-gapped. The web UI is not "
            f"reachable from a separate laptop.{_RESET}"
        )
    lines.extend([
        "",
        f"  {_DIM}Refresh:{_RESET} run {_BOLD}driveforge-field-report{_RESET} again "
        f"after plugging or unplugging drives.",
        f"  {_DIM}Quit:{_RESET}    power off the server.",
        f"{_BOLD}{_BLUE}{_hr()}{_RESET}",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------- Data collection


def _collect_drive_rows() -> list[dict]:
    """Discover drives, probe SMART on each, run grading. Returns a
    flat list of dicts the renderer can iterate cleanly."""
    drives = drive_mod.discover()
    rows: list[dict] = []
    for d in drives:
        is_vd = server_info.is_raid_virtual_disk_model(d.model or "")
        row: dict = {
            "serial": d.serial,
            "model": d.model,
            "capacity_tb": d.capacity_tb,
            "type": (
                "HDD" if (d.rotation_rate and d.rotation_rate > 0)
                else "SSD" if d.rotation_rate == 0
                else "—"
            ),
            "is_virtual_disk": is_vd,
            "grade": None,
            "ceiling_reason": None,
            "error": None,
        }
        if is_vd:
            # Don't even try to SMART-probe a RAID virtual disk —
            # smartctl will return garbage or "no SMART support" and
            # confuse the report. Caller renders "(RAID volume)" instead.
            rows.append(row)
            continue
        try:
            snap = smart_mod.snapshot(d.device_path)
            from driveforge import config as cfg
            grading_cfg = cfg.GradingConfig()
            dclass = classify(
                model=d.model,
                transport=(
                    d.transport.value if hasattr(d.transport, "value")
                    else str(d.transport)
                ),
                rotation_rate=d.rotation_rate,
            )
            result = grading.grade_drive(
                pre=snap, post=snap, config=grading_cfg,
                short_test_passed=True, long_test_passed=True,
                drive_class=dclass,
            )
            row["grade"] = result.grade
            # Pluck the strictest ceiling reason for a one-line
            # explanation next to the grade letter.
            for rule in result.rules or []:
                forces = rule.forces_grade
                if forces in ("B", "C"):
                    detail = rule.detail or ""
                    # Strip trailing "— capped at X" tails (the grade
                    # letter shown next to it makes the cap obvious).
                    for tail in (" — capped at C", " — capped at B"):
                        if detail.endswith(tail):
                            detail = detail[: -len(tail)]
                            break
                    row["ceiling_reason"] = detail
                    break
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("field-report: SMART probe failed for %s", d.serial)
        rows.append(row)
    return rows


def _detect_daemon_url() -> str | None:
    """Best-effort detection of a reachable URL for the local daemon.
    Picks the first non-loopback IPv4 + the configured daemon port.
    Returns None when no non-loopback address is up (air-gapped case)."""
    import socket
    import subprocess
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=2,
        )
        for line in out.stdout.splitlines():
            # "2: enp1s0    inet 192.168.1.42/24 brd ..."
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    ip = parts[i + 1].split("/")[0]
                    return f"http://{ip}:8080"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    # Fallback: try socket-based discovery (works in test envs without `ip`)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("192.0.2.1", 1))  # TEST-NET-1, no packet sent
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return f"http://{ip}:8080"
    except OSError:
        pass
    return None


# ---------------------------------------------------------------- Entry point


def render() -> str:
    """Build the entire report as a single string. Separated from
    `main()` so tests can assert on the rendered output without
    capturing stdout."""
    info = server_info.collect()
    daemon_url = _detect_daemon_url()
    drive_rows = _collect_drive_rows()

    sections: list[str] = []
    sections.append(_render_header())
    sections.append("")
    sections.append(_render_server(info))
    raid_warning = _render_raid_warning(info)
    if raid_warning:
        sections.append(raid_warning)
    sections.append("")
    sections.append(_render_drives(drive_rows))
    sections.append(_render_footer(daemon_url))
    return "\n".join(sections)


def main() -> int:
    """Print the report. Returns shell exit code (0 always — even if
    drive probes individually fail, the report itself rendered)."""
    try:
        print(render())
    except Exception as exc:  # noqa: BLE001
        # Last-ditch fallback so a broken field-check doesn't leave
        # the operator staring at a Python traceback.
        print(f"\n{_RED}field-report failed: {exc}{_RESET}\n", file=sys.stderr)
        logger.exception("field-report: top-level render failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
