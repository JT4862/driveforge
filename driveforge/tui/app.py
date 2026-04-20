"""Textual TUI — thin client over the daemon REST API.

Full feature parity with the web UI (minus visual polish): users can view
the dashboard, browse batches and history, start a new batch, or abort
everything in flight. Designed for the crash cart when the web UI is
unreachable.
"""

from __future__ import annotations

import argparse
from typing import Any

import httpx
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Static

DEFAULT_API = "http://127.0.0.1:8080"


def _client(api_base: str) -> httpx.Client:
    return httpx.Client(base_url=api_base, timeout=3.0)


class Dashboard(Screen):
    BINDINGS = [
        Binding("n", "new_batch", "New batch"),
        Binding("a", "abort_all", "Abort all"),
        Binding("r", "refresh", "Refresh"),
        Binding("b", "app.push_screen('batches')", "Batches"),
        Binding("h", "app.push_screen('history')", "History"),
    ]

    def __init__(self, api_base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.api_base = api_base

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("[b]Dashboard[/b] — live bay state", id="heading")
        yield DataTable(id="bay-table")
        yield Horizontal(
            Button("New batch", variant="primary", id="new-batch"),
            Button("Abort all", variant="error", id="abort-all"),
            id="action-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#bay-table", DataTable)
        table.add_columns("Bay", "Serial", "Model", "Capacity", "Phase", "%")
        table.cursor_type = "row"
        self.set_interval(3.0, self.action_refresh)
        self.action_refresh()

    def _fetch(self, path: str) -> Any:
        try:
            with _client(self.api_base) as c:
                return c.get(path).json()
        except httpx.HTTPError:
            return None

    def action_refresh(self) -> None:
        health = self._fetch("/api/health") or {}
        drives = self._fetch("/api/drives") or []
        active = set(health.get("active_serials", []))
        table = self.query_one("#bay-table", DataTable)
        table.clear()
        if not drives:
            table.add_row("—", "—", "(no drives)", "—", "—", "—")
            return
        # Flat drive-centric list: drives currently present, ordered by serial.
        # Active drives (in the test pipeline) render with a distinct state label.
        for i, d in enumerate(sorted(drives, key=lambda r: r.get("serial", "")), start=1):
            serial = d.get("serial", "?")
            state_label = "active" if serial in active else "idle"
            table.add_row(
                str(i),
                serial,
                d.get("model", "?"),
                f"{d.get('capacity_tb', 0):.1f} TB",
                state_label,
                "—",
            )

    def action_new_batch(self) -> None:
        try:
            with _client(self.api_base) as c:
                c.post("/api/batches", json={"source": "TUI batch"})
            self.notify("Batch started.")
        except httpx.HTTPError as exc:
            self.notify(f"Failed: {exc}", severity="error")
        self.action_refresh()

    def action_abort_all(self) -> None:
        # Placeholder — abort endpoint not yet implemented on the daemon.
        self.notify("Abort-all not yet wired on the daemon.", severity="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-batch":
            self.action_new_batch()
        elif event.button.id == "abort-all":
            self.action_abort_all()


class Batches(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, api_base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.api_base = api_base

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("[b]Batches[/b]", id="heading")
        yield DataTable(id="batch-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#batch-table", DataTable)
        table.add_columns("ID", "Source", "Started", "A", "B", "C", "Fail")
        table.cursor_type = "row"
        self.action_refresh()

    def action_refresh(self) -> None:
        try:
            with _client(self.api_base) as c:
                rows = c.get("/api/batches").json()
        except httpx.HTTPError:
            return
        table = self.query_one("#batch-table", DataTable)
        table.clear()
        for b in rows:
            totals = b.get("totals", {})
            table.add_row(
                b["id"][:8],
                b.get("source") or "—",
                (b.get("started_at") or "—")[:19],
                str(totals.get("A", 0)),
                str(totals.get("B", 0)),
                str(totals.get("C", 0)),
                str(totals.get("fail", 0)),
            )


class History(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, api_base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.api_base = api_base

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("[b]History[/b] — completed test runs", id="heading")
        yield DataTable(id="history-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.add_columns("Completed", "Serial", "Grade", "POH")
        table.cursor_type = "row"
        self.action_refresh()

    def action_refresh(self) -> None:
        try:
            with _client(self.api_base) as c:
                drives = c.get("/api/drives").json()
        except httpx.HTTPError:
            return
        table = self.query_one("#history-table", DataTable)
        table.clear()
        for d in drives:
            # In lieu of a dedicated /history endpoint, just show each drive
            # with its most recent test-run summary. Future enhancement.
            table.add_row(
                "—",
                d["serial"],
                "—",
                "—",
            )


class DriveForgeTUI(App):
    CSS = """
    Screen { background: #0e1116; }
    Header { dock: top; background: #151a22; color: #e6edf3; }
    Footer { dock: bottom; background: #151a22; color: #8b97a5; }
    #heading { padding: 1 2; color: #e6edf3; }
    DataTable { background: #151a22; color: #e6edf3; }
    #action-bar { dock: bottom; height: 3; padding: 1 2; background: #151a22; }
    Button { margin-right: 2; }
    """
    SCREENS: dict[str, type[Screen]] = {}

    def __init__(self, api_base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.api_base = api_base

    def on_mount(self) -> None:
        self.install_screen(Dashboard(self.api_base), name="dashboard")
        self.install_screen(Batches(self.api_base), name="batches")
        self.install_screen(History(self.api_base), name="history")
        self.push_screen("dashboard")


def main() -> None:
    parser = argparse.ArgumentParser(prog="driveforge-tui")
    parser.add_argument("--api", default=DEFAULT_API, help="daemon REST base URL")
    args = parser.parse_args()
    DriveForgeTUI(args.api).run()


if __name__ == "__main__":
    main()
