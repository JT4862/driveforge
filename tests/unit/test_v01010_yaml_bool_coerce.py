"""v0.10.10 — tolerate YAML's bare-bool footgun on auto_enroll_mode.

YAML 1.1 treats bare `off` / `on` / `yes` / `no` as booleans.
`auto_enroll_mode: off` in a hand-edited /etc/driveforge/driveforge.yaml
therefore becomes Python `False`, which pydantic rejects for a `str`
field and crash-loops the daemon.

The field validator coerces False → "off" and True → "full" so
either form works.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from driveforge import config as cfg


def test_bool_false_coerces_to_off(tmp_path: Path) -> None:
    p = tmp_path / "driveforge.yaml"
    p.write_text(yaml.safe_dump({"daemon": {"auto_enroll_mode": False}}))
    s = cfg.load(p)
    assert s.daemon.auto_enroll_mode == "off"


def test_bool_true_coerces_to_full(tmp_path: Path) -> None:
    """Symmetry: `on`/`yes`/`true` → "full" so a casually-edited
    YAML flip to 'on' at least gives the user *something* instead
    of crashing."""
    p = tmp_path / "driveforge.yaml"
    p.write_text(yaml.safe_dump({"daemon": {"auto_enroll_mode": True}}))
    s = cfg.load(p)
    assert s.daemon.auto_enroll_mode == "full"


def test_bare_off_keyword_in_yaml_roundtrips(tmp_path: Path) -> None:
    """End-to-end — write the exact form JT hit
    (`auto_enroll_mode: off` without quotes) and confirm the daemon
    can still load."""
    p = tmp_path / "driveforge.yaml"
    p.write_text("daemon:\n  auto_enroll_mode: off\n")
    s = cfg.load(p)
    assert s.daemon.auto_enroll_mode == "off"


def test_string_off_still_works(tmp_path: Path) -> None:
    """Quoted / canonical form must not regress."""
    p = tmp_path / "driveforge.yaml"
    p.write_text('daemon:\n  auto_enroll_mode: "off"\n')
    s = cfg.load(p)
    assert s.daemon.auto_enroll_mode == "off"


def test_string_quick_still_works(tmp_path: Path) -> None:
    p = tmp_path / "driveforge.yaml"
    p.write_text('daemon:\n  auto_enroll_mode: "quick"\n')
    s = cfg.load(p)
    assert s.daemon.auto_enroll_mode == "quick"
