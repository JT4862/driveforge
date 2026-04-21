"""Guardrail tests for v0.6.9's install.sh changes.

install.sh is a shell script — we don't run it end-to-end in unit
tests (that's integration territory; it needs a real Debian host with
systemd, policykit, udev, etc.). Instead, we parse the script as text
and assert the key imperatives for v0.6.9 are present. If a future
refactor accidentally drops the Debian-hdparm-rule mask, these tests
catch it before the next hardware session reproduces the D-state
pileup.

Background on the mask (full writeup in install.sh's inline comment):
Debian's `hdparm` package ships /lib/udev/rules.d/85-hdparm.rules,
which fires /lib/udev/hdparm on every `add` event for sd[a-z] devices.
That helper issues `hdparm -B<N>` to set APM — which on a drive
mid-SECURITY-ERASE-UNIT goes D-state because the drive doesn't answer
APM commands during an erase. Repeated udev re-enumeration transitions
during an erase stack multiple D-state `hdparm -B254` processes,
which then starve the mpt2sas HBA firmware-event kworker and block
all new drive discovery on that HBA. Masking the Debian rule removes
the source of those spurious hdparm calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"


@pytest.fixture(scope="module")
def install_sh_text() -> str:
    assert INSTALL_SH.is_file(), f"install.sh not found at {INSTALL_SH}"
    return INSTALL_SH.read_text()


def test_install_sh_masks_debian_hdparm_udev_rule(install_sh_text: str) -> None:
    """Mask must drop a symlink to /dev/null at the /etc/udev path that
    overrides /lib/udev/rules.d/85-hdparm.rules. Symlink to /dev/null
    is the standard Debian idiom for disabling a vendor-shipped udev
    rule — it's read by udev as an empty ruleset and shadows the
    /lib/udev version by name+priority."""
    assert "85-hdparm.rules" in install_sh_text, (
        "install.sh lost its reference to 85-hdparm.rules — the mask block was removed"
    )
    assert "ln -sf /dev/null /etc/udev/rules.d/85-hdparm.rules" in install_sh_text, (
        "install.sh must symlink /etc/udev/rules.d/85-hdparm.rules → /dev/null to "
        "shadow the Debian-shipped rule. If the implementation changed shape, "
        "update this assertion to match — do NOT remove the mask itself."
    )


def test_install_sh_reloads_udev_after_masking(install_sh_text: str) -> None:
    """Masking the rule on disk doesn't retroactively cancel an
    already-loaded ruleset — udev needs a reload. Be tolerant of
    exact form (the existing Brother USB rule block already reloads,
    so the hdparm block may piggyback on that), but at minimum the
    `udevadm control --reload-rules` invocation must be present
    somewhere after the mask line."""
    mask_idx = install_sh_text.find("ln -sf /dev/null /etc/udev/rules.d/85-hdparm.rules")
    assert mask_idx != -1, "mask line missing (covered by previous test; fail here too for clarity)"
    tail = install_sh_text[mask_idx:]
    assert "udevadm control --reload-rules" in tail, (
        "no `udevadm control --reload-rules` after the hdparm mask line — "
        "the mask won't take effect until a reboot otherwise"
    )


def test_install_sh_mask_is_idempotent(install_sh_text: str) -> None:
    """install.sh runs on every update (via driveforge-update.service).
    The mask install must be safe to run N times without error. The
    `[[ ! -L ... ]]` guard ensures we only create the symlink if it's
    not already there; without that, `ln -sf` works anyway but the
    idempotence intent should be explicit in the script for
    readability + future-proofing."""
    assert "[[ ! -L /etc/udev/rules.d/85-hdparm.rules ]]" in install_sh_text, (
        "idempotence guard missing — the mask block should skip re-creating "
        "the symlink if it's already in place"
    )


def test_install_sh_installs_udev_restart_unit(install_sh_text: str) -> None:
    """v0.6.9's in-app udev-restart button relies on a dedicated
    systemd unit (driveforge-udev-restart.service). install.sh must
    copy it into /etc/systemd/system alongside the update unit.
    Without this, the polkit rule we install would whitelist a unit
    that doesn't exist on disk, and `systemctl start` would fail
    with 'Unit not found'."""
    assert "driveforge-udev-restart.service" in install_sh_text, (
        "install.sh lost its install step for driveforge-udev-restart.service"
    )


def test_install_sh_installs_udev_restart_polkit_rule(install_sh_text: str) -> None:
    """Paired with the systemd unit above. install.sh must drop the
    polkit rule into /etc/polkit-1/rules.d/ so the daemon user can
    actually start the unit. Rule number 51 keeps it adjacent to the
    v0.6.0 update rule (50)."""
    assert "51-driveforge-udev-restart.rules" in install_sh_text, (
        "install.sh lost its install step for the udev-restart polkit rule"
    )


def test_install_sh_mask_has_rationale_comment(install_sh_text: str) -> None:
    """Future maintainers reading install.sh should be able to
    understand WHY the mask exists without needing to find the
    backlog. The block must include enough comment to reconstruct
    the problem class (D-state hdparm -B254 during secure_erase).
    This is a weak check — just confirms the key phrase is present."""
    assert "hdparm -B254" in install_sh_text or "B254" in install_sh_text, (
        "install.sh's mask block should mention hdparm -B254 so the "
        "rationale is discoverable in-context"
    )
    assert "secure" in install_sh_text.lower(), (
        "install.sh's mask block should mention the secure_erase interaction "
        "that motivates the mask"
    )
