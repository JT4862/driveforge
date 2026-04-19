"""Textual TUI — thin client over the daemon REST API.

Same nav structure as the web UI: Dashboard / Batches / History / Settings.
Primary use case is the crash cart when the web UI isn't reachable.
"""

from __future__ import annotations

import argparse
from typing import Any

import httpx
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import DataTable, Footer, Header, Static

DEFAULT_API = "http://127.0.0.1:8080"


class DashboardView(Container):
    DEFAULT_CSS = """
    DashboardView { height: 1fr; }
    #bay-table { height: 1fr; }
    """

    def __init__(self, api_base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.api_base = api_base

    def compose(self) -> ComposeResult:
        yield Static("[bold]Dashboard[/bold] — live bay state", id="heading")
        yield DataTable(id="bay-table")

    def on_mount(self) -> None:
        table = self.query_one("#bay-table", DataTable)
        table.add_columns("Bay", "Serial", "Model", "Capacity", "Phase", "%")
        self.set_interval(3.0, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        try:
            resp = httpx.get(f"{self.api_base}/api/health", timeout=2.0)
            bay_assignments = resp.json().get("bay_assignments", {})
            drives_resp = httpx.get(f"{self.api_base}/api/drives", timeout=2.0)
            by_serial = {d["serial"]: d for d in drives_resp.json()}
        except httpx.HTTPError:
            return
        table = self.query_one("#bay-table", DataTable)
        table.clear()
        for bay in range(1, 9):
            serial = bay_assignments.get(str(bay)) or bay_assignments.get(bay)
            if not serial:
                table.add_row(str(bay), "—", "(empty)", "—", "—", "—")
                continue
            d = by_serial.get(serial, {})
            table.add_row(
                str(bay),
                serial,
                d.get("model", "?"),
                f"{d.get('capacity_tb', 0):.1f} TB",
                "active",
                "—",
            )


class DriveForgeTUI(App):
    CSS = """
    Screen { layout: vertical; }
    Header { dock: top; }
    Footer { dock: bottom; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh")]

    def __init__(self, api_base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.api_base = api_base

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DashboardView(self.api_base)
        yield Footer()

    def action_refresh(self) -> None:
        self.query_one(DashboardView)._refresh()


def main() -> None:
    parser = argparse.ArgumentParser(prog="driveforge-tui")
    parser.add_argument("--api", default=DEFAULT_API, help="daemon REST base URL")
    args = parser.parse_args()
    DriveForgeTUI(args.api).run()


if __name__ == "__main__":
    main()
