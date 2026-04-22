"""Tests for v0.7.0's network label printer support.

`brother_ql` already ships a network backend — v0.7.0 wires it into
DriveForge's Settings flow. Key invariants:

  1. `PrinterConfig.network_host` / `network_port` persist across
     Settings edits even if operator toggles connection=usb and back.
  2. Saving with connection=network synthesizes
     `backend_identifier = "tcp://<host>:<port>"` so the low-level
     `core/printer.py:print_label` call-site stays agnostic of the
     two-fields-vs-one storage detail.
  3. Switching back to USB clears a stale tcp:// identifier so
     pyusb auto-discovery fires again cleanly.
  4. `_BROTHER_QL_BACKENDS` maps connection=network → backend="network"
     — the same map the pyusb path uses.

USB support is unchanged — regression-guarded by the existing
`tests/unit/test_printer*.py` suite.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.core.printer import _BROTHER_QL_BACKENDS


# ----------------------------------------------------------- config schema


def test_printer_config_has_network_fields_with_defaults() -> None:
    """PrinterConfig gained network_host + network_port in v0.7.0.
    Defaults are "no host configured, port 9100" so a fresh install
    doesn't advertise a bogus network target."""
    p = cfg.PrinterConfig()
    assert p.network_host is None
    assert p.network_port == 9100
    # Default connection stays USB so existing installs don't flip.
    assert p.connection == "usb"


def test_brother_ql_backends_map_includes_network() -> None:
    """Sanity: the connection-type → brother_ql-backend-id map must
    route 'network' to the library's 'network' backend. Without this,
    print_label would dispatch to the wrong transport on a network-
    configured printer."""
    assert _BROTHER_QL_BACKENDS["network"] == "network"
    # Also confirm USB mapping preserved.
    assert _BROTHER_QL_BACKENDS["usb"] == "pyusb"


# -------------------------------------------------------- save-route logic


def _bootstrap_app(tmp_path):
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True

    app = make_app(settings)
    DaemonState.boot(settings)
    return app


def test_save_printer_synthesizes_tcp_identifier_on_network(tmp_path) -> None:
    """POST /settings/printer with connection=network + host + port
    must produce backend_identifier='tcp://host:port'. This is what
    brother_ql's network backend expects."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/settings/printer",
            data={
                "model": "QL-820NWB",
                "connection": "network",
                "label_roll": "DK-1209",
                "network_host": "10.10.10.42",
                "network_port": "9100",
                "auto_print": "on",
            },
        )

    assert resp.status_code == 303
    p = state.settings.printer
    assert p.connection == "network"
    assert p.network_host == "10.10.10.42"
    assert p.network_port == 9100
    assert p.backend_identifier == "tcp://10.10.10.42:9100"


def test_save_printer_round_trips_network_fields_while_on_usb(tmp_path) -> None:
    """Switching connection=usb must NOT wipe network_host/port —
    operators may switch back later. Only backend_identifier gets
    cleared (and only if it was stale tcp://)."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()

    with TestClient(app, follow_redirects=False) as client:
        # Round 1: save as network.
        client.post(
            "/settings/printer",
            data={
                "model": "QL-820NWB",
                "connection": "network",
                "network_host": "192.168.1.50",
                "network_port": "9100",
            },
        )
        # Round 2: switch to USB. Network host/port should persist;
        # backend_identifier's stale tcp:// should be cleared so the
        # auto-discover path fires.
        client.post(
            "/settings/printer",
            data={
                "model": "QL-820NWB",
                "connection": "usb",
                "network_host": "192.168.1.50",  # form still carries it
                "network_port": "9100",
            },
        )

    p = state.settings.printer
    assert p.connection == "usb"
    assert p.network_host == "192.168.1.50", "network host must round-trip across usb edit"
    assert p.network_port == 9100
    # Stale tcp:// identifier cleared.
    assert p.backend_identifier is None


def test_save_printer_preserves_non_tcp_backend_identifier_on_switch(tmp_path) -> None:
    """Operators who manually filled in backend_identifier for a USB
    printer (`usb://0x04f9:0x...`) must not have it cleared when
    saving. Only tcp:// values get the stale-clear treatment."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    # Pre-seed the state — the form doesn't carry backend_identifier,
    # so the route leaves it alone except for the tcp:// clear path.
    state.settings.printer.backend_identifier = "usb://0x04f9:0x209d"

    with TestClient(app, follow_redirects=False) as client:
        client.post(
            "/settings/printer",
            data={
                "model": "QL-820NWB",
                "connection": "usb",
            },
        )

    assert state.settings.printer.backend_identifier == "usb://0x04f9:0x209d"


def test_save_printer_handles_non_numeric_port_gracefully(tmp_path) -> None:
    """A typo in the port field shouldn't 500 the save. Fall back to
    the default 9100 — the operator sees the default repopulated on
    the next render + can retype."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/settings/printer",
            data={
                "model": "QL-820NWB",
                "connection": "network",
                "network_host": "10.10.10.42",
                "network_port": "nine-one-hundred",
            },
        )

    assert resp.status_code == 303
    assert state.settings.printer.network_port == 9100
