"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from driveforge.core.process import FixtureRunner, set_fixture_runner

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _install_fixture_runner():
    set_fixture_runner(FixtureRunner(FIXTURES_DIR))
    yield
    set_fixture_runner(None)
