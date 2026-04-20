"""Smoke tests for the setup-wizard module.

The wizard's request handlers are lightly tested — we don't exercise
full HTMX rendering, but we do call the helpers so missing imports or
signature regressions fail at unit-test time instead of on a live
request.
"""

from __future__ import annotations

from driveforge.web import setup


def test_network_snapshot_returns_expected_keys() -> None:
    """Regression guard — the helper was missing its `run` import after a
    refactor and blew up at request time with NameError. Calling it here
    catches that class of regression at test time."""
    snap = setup._network_snapshot()
    assert set(snap.keys()) == {"hostname", "ip", "dhcp"}
    assert snap["hostname"]  # non-empty string
    # ip can legitimately be None (no egress on CI runner); dhcp is one
    # of three strings.
    assert snap["dhcp"] in {"unknown", "likely DHCP"}
