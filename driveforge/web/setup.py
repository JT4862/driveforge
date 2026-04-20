"""First-run setup wizard.

Five-step flow that auto-detects wherever possible and skips nothing it
can answer itself. Triggered whenever `setup_completed` is False — users
can replay it later from Settings.

Steps:
1. Welcome
2. Hardware & network discovery (informational)
3. Printer (model dropdown, auto-detected if udev saw it)
4. Grading thresholds (defaults preselected, inline editor)
5. Integrations (webhook URL + Cloudflare Tunnel, each skippable)
"""

from __future__ import annotations

import platform
import socket
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from driveforge import config as cfg
from driveforge.core import drive as drive_mod
from driveforge.core import telemetry
from driveforge.core.process import run
from driveforge.daemon.state import get_state  # noqa: F401 (used via ctx)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)
router = APIRouter(prefix="/setup")

STEP_TITLES = {
    1: "Welcome",
    2: "Hardware & network",
    3: "Printer",
    4: "Grading thresholds",
    5: "Integrations",
}
TOTAL_STEPS = 5


def _network_snapshot() -> dict:
    """Quick, cheap probe of host networking state for the wizard."""
    hostname = socket.gethostname()
    ip = None
    try:
        # Trick to find the primary egress IP without sending a packet
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    dhcp = "unknown"
    if platform.system() == "Linux":
        result = run(["ip", "route", "get", "1.1.1.1"])
        dhcp = "likely DHCP" if "dhcp" in result.stdout.lower() else "unknown"
    return {"hostname": hostname, "ip": ip, "dhcp": dhcp}


@router.get("", response_class=HTMLResponse)
def setup_root(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/setup/1", status_code=303)


@router.get("/{step}", response_class=HTMLResponse)
def setup_step(request: Request, step: int) -> HTMLResponse:
    state = get_state()
    ctx: dict = {
        "step": step,
        "total": TOTAL_STEPS,
        "title": STEP_TITLES.get(step, "Setup"),
        "settings": state.settings,
    }
    if step == 2:
        ctx["drives"] = drive_mod.discover()
        ctx["network"] = _network_snapshot()
        # Fresh capability probe on each wizard view so the operator sees
        # what the daemon can actually do right now (e.g. just fixed
        # /dev/ipmi0 perms, or just plugged in a drive to populate LED slot
        # mapping). Also refreshes bay_plan as a side effect.
        state.refresh_bay_plan()
        ctx["capabilities"] = state.capabilities
        # Surface a sample chassis temperature reading when available, so
        # the wizard confirms not just "BMC reachable" but "real data
        # coming back." Skipped silently when the capability is False.
        if state.capabilities.chassis_temperature:
            ctx["chassis_temps"] = telemetry.read_chassis_temperatures()
        else:
            ctx["chassis_temps"] = {}
    elif step == 3:
        ctx["printer_models"] = [
            "QL-800",
            "QL-810W",
            "QL-820NWBc",
            "QL-1100",
            "QL-1110NWBc",
        ]
        ctx["label_rolls"] = ["DK-1209", "DK-1208", "DK-1201", "DK-1221", "DK-22210"]
    return templates.TemplateResponse(request, f"setup/step{step}.html", ctx)


@router.post("/{step}")
async def setup_submit(request: Request, step: int) -> RedirectResponse:
    state = get_state()
    form = await request.form()
    settings = state.settings

    if step == 3:
        model = (form.get("printer_model") or "").strip()
        settings.printer.model = model or None
        settings.printer.label_roll = (form.get("label_roll") or "").strip() or None
    elif step == 4:
        for key in (
            "grade_a_reallocated_max",
            "grade_b_reallocated_max",
            "grade_c_reallocated_max",
        ):
            v = form.get(key)
            if v:
                setattr(settings.grading, key, int(v))
        settings.grading.fail_on_pending_sectors = form.get("fail_on_pending_sectors") == "on"
        settings.grading.fail_on_offline_uncorrectable = (
            form.get("fail_on_offline_uncorrectable") == "on"
        )
        temp_str = (form.get("thermal_excursion_c") or "").strip()
        settings.grading.thermal_excursion_c = int(temp_str) if temp_str else None
    elif step == 5:
        settings.integrations.webhook_url = (form.get("webhook_url") or "").strip() or None
        settings.integrations.cloudflare_tunnel_hostname = (
            (form.get("cloudflare_tunnel_hostname") or "").strip() or None
        )

    next_step = step + 1
    if next_step > TOTAL_STEPS:
        settings.setup_completed = True
        try:
            cfg.save(settings)
        except PermissionError:
            # Dev-mode path; config writes land under /etc which isn't writable
            # without the installer. The in-memory setting still takes effect.
            pass
        return RedirectResponse(url="/", status_code=303)
    try:
        cfg.save(settings)
    except PermissionError:
        pass
    return RedirectResponse(url=f"/setup/{next_step}", status_code=303)
