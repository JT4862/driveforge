"""Tests for v0.6.0's polkit-mediated `trigger_in_app_update()`.

v0.6.0 drops the `sudo -n` prefix from the argv and leans on a polkit
rule to authorize the daemon user's `StartUnit` call on
driveforge-update.service. systemctl talks to systemd1 over D-Bus
under the hood; polkit mediates. Net effect: no more 10-second
sudo/PAM reverse-DNS timeouts on hosts like the R720 where that
path misbehaved (see backlog v0.5.4 entry).

Tests here verify the argv shape + error-handling contract, not the
polkit rule itself — the rule is JavaScript data, not Python code,
and its correctness can only be validated end-to-end on a real
Debian host (see the v0.6.0 validation step in the backlog).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

from driveforge.core.updates import trigger_in_app_update, UPDATE_SERVICE


def test_argv_does_not_include_sudo() -> None:
    """The smoking-gun regression test for v0.6.0. If a future refactor
    accidentally puts `sudo` back into the argv, this test fails —
    which matters because re-introducing sudo re-introduces the whole
    class of PAM/reverse-DNS timeout bugs v0.6.0 exists to fix."""
    fake_proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=fake_proc) as run_mock, \
         patch("shutil.which", return_value="/usr/bin/systemctl"):
        ok, _msg = trigger_in_app_update()
    assert ok is True
    assert run_mock.call_count == 1
    argv = run_mock.call_args.args[0]
    assert "sudo" not in argv[0].lower()
    assert not any("sudo" in str(arg).lower() for arg in argv), (
        f"argv must not contain sudo (v0.6.0 polkit refactor); got {argv}"
    )


def test_argv_is_systemctl_start_update_service() -> None:
    """The only thing we ask systemctl to do is `start` the specific
    update unit. Any drift here — wrong verb, wrong unit name —
    would also drift from what the polkit rule whitelists, and the
    call would fail on real hardware with a confusing "authentication
    required" error."""
    fake_proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=fake_proc) as run_mock, \
         patch("shutil.which", return_value="/usr/bin/systemctl"):
        trigger_in_app_update()
    argv = run_mock.call_args.args[0]
    # Exactly three args: [systemctl_path, "start", unit_name]. No
    # flags, no sudo wrapper, no nothing.
    assert len(argv) == 3, f"expected 3-element argv, got {argv}"
    assert argv[0].endswith("systemctl")
    assert argv[1] == "start"
    assert argv[2] == UPDATE_SERVICE
    assert argv[2] == "driveforge-update.service"  # exact string the polkit rule matches


def test_non_zero_exit_returns_failure_with_stderr() -> None:
    """When the polkit rule is missing or mis-installed, systemctl
    exits non-zero with a polkit-specific error on stderr. The whole
    point of returning that stderr verbatim is so the Settings banner
    can show the operator WHY — "Interactive authentication required"
    is the obvious tell for a missing/broken polkit rule."""
    fake_proc = MagicMock(
        returncode=1,
        stdout="",
        stderr="Failed to start driveforge-update.service: Interactive authentication required.",
    )
    with patch("subprocess.run", return_value=fake_proc), \
         patch("shutil.which", return_value="/usr/bin/systemctl"):
        ok, msg = trigger_in_app_update()
    assert ok is False
    assert "Interactive authentication required" in msg
    assert "rc=1" in msg


def test_timeout_is_caught_and_reported() -> None:
    """A 10-second subprocess timeout (same value as pre-v0.6.0, even
    though polkit itself shouldn't ever need that long) must not raise
    — catch it and return a descriptive failure message. The caller's
    HTTP handler redirects to ?install_error=... and surfaces it in
    the Settings banner."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=10),
    ), patch("shutil.which", return_value="/usr/bin/systemctl"):
        ok, msg = trigger_in_app_update()
    assert ok is False
    assert "failed to invoke" in msg.lower()


def test_success_returns_expected_message() -> None:
    """Positive-path sanity: exit 0 → (True, human-readable message
    mentioning the unit name) so the Settings page renders a live-
    log streaming panel rather than the error banner."""
    fake_proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=fake_proc), \
         patch("shutil.which", return_value="/usr/bin/systemctl"):
        ok, msg = trigger_in_app_update()
    assert ok is True
    assert UPDATE_SERVICE in msg
