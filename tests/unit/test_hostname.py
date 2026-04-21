"""Tests for driveforge.core.hostname (v0.2.8).

Covers:
  - validate_hostname's accept/reject rules (RFC 1123 single-label)
  - current_hostname reads /etc/hostname / falls back to socket
  - apply_hostname(dev_mode=True) is a pure validator (no system writes)
  - _patch_etc_hosts rewrites 127.0.1.1 in place or appends when absent
"""

from __future__ import annotations

from pathlib import Path

import pytest

from driveforge.core import hostname as hn


# ---------------------------------------------------------------- validate


@pytest.mark.parametrize(
    "name,expected",
    [
        ("driveforge", "driveforge"),
        ("DriveForge", "driveforge"),           # lowercased
        ("  forge-rack-b  ", "forge-rack-b"),    # whitespace stripped
        ("f", "f"),                              # single char OK
        ("a" * 63, "a" * 63),                    # 63 chars OK
        ("forge-01", "forge-01"),
        ("FORGE", "forge"),
        ("node1", "node1"),
    ],
)
def test_validate_hostname_accepts(name: str, expected: str) -> None:
    assert hn.validate_hostname(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "",                      # empty
        "   ",                   # whitespace only
        "a" * 64,                # too long
        "-leadinghyphen",        # leading hyphen
        "trailinghyphen-",       # trailing hyphen
        "has_underscore",        # underscore not allowed
        "has spaces",            # spaces not allowed
        "has.dot",               # single-label only
        "12345",                 # all digits banned
        "localhost",             # reserved
        "localdomain",           # reserved
        "ip6-localhost",         # reserved
        "ünicode",              # non-ASCII
    ],
)
def test_validate_hostname_rejects(name: str) -> None:
    with pytest.raises(hn.HostnameError):
        hn.validate_hostname(name)


def test_validate_hostname_requires_input() -> None:
    with pytest.raises(hn.HostnameError):
        hn.validate_hostname(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- current


def test_current_hostname_reads_etc_hostname(tmp_path, monkeypatch) -> None:
    fake = tmp_path / "hostname"
    fake.write_text("forge-lab\n", encoding="utf-8")
    monkeypatch.setattr(hn, "Path", lambda p: fake if p == "/etc/hostname" else Path(p))
    assert hn.current_hostname() == "forge-lab"


def test_current_hostname_falls_back_to_socket(monkeypatch) -> None:
    """If /etc/hostname is missing or unreadable, we fall back to
    socket.gethostname() — the unit test host's name is fine here, we
    just want to confirm the call doesn't raise."""
    monkeypatch.setattr(
        hn, "Path", lambda p: Path("/nonexistent-for-test") if p == "/etc/hostname" else Path(p)
    )
    result = hn.current_hostname()
    # Just assert it returned a string (possibly empty). Content varies by host.
    assert isinstance(result, str)


# ---------------------------------------------------------------- apply


def test_apply_hostname_dev_mode_is_no_op(caplog) -> None:
    """dev_mode=True short-circuits after validation — must not call
    hostnamectl or touch /etc. Validates + returns the normalized name."""
    import logging
    caplog.set_level(logging.INFO)
    result = hn.apply_hostname("  NewName  ", dev_mode=True)
    assert result == "newname"
    # log line confirms the no-op branch ran
    assert any("apply_hostname(dev)" in rec.message for rec in caplog.records)


def test_apply_hostname_rejects_invalid_input() -> None:
    with pytest.raises(hn.HostnameError):
        hn.apply_hostname("bad hostname with spaces", dev_mode=True)


# ---------------------------------------------------------------- hosts file


def test_patch_etc_hosts_rewrites_existing_row(tmp_path, monkeypatch) -> None:
    """When 127.0.1.1 already points at an old hostname, we rewrite the
    row in place and leave everything else alone."""
    hosts = tmp_path / "hosts"
    hosts.write_text(
        "127.0.0.1\tlocalhost\n"
        "127.0.1.1\told-name\n"
        "::1\t\tip6-localhost ip6-loopback\n"
        "ff02::1\t\tip6-allnodes\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hn, "Path", lambda p: hosts if p == "/etc/hosts" else Path(p))
    hn._patch_etc_hosts("new-name")
    lines = hosts.read_text(encoding="utf-8").splitlines()
    assert "127.0.0.1\tlocalhost" in lines
    assert "127.0.1.1\tnew-name" in lines
    assert "127.0.1.1\told-name" not in lines
    assert any("ip6-localhost" in line for line in lines)


def test_patch_etc_hosts_appends_when_row_missing(tmp_path, monkeypatch) -> None:
    """On a system without a 127.0.1.1 row we add one. Preserves the
    rest of /etc/hosts verbatim."""
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n::1\tip6-localhost\n", encoding="utf-8")
    monkeypatch.setattr(hn, "Path", lambda p: hosts if p == "/etc/hosts" else Path(p))
    hn._patch_etc_hosts("freshbox")
    content = hosts.read_text(encoding="utf-8")
    assert "127.0.0.1\tlocalhost" in content
    assert "127.0.1.1\tfreshbox" in content


def test_patch_etc_hosts_leaves_commented_rows_alone(tmp_path, monkeypatch) -> None:
    """Don't rewrite a commented-out 127.0.1.1 example."""
    hosts = tmp_path / "hosts"
    hosts.write_text(
        "127.0.0.1\tlocalhost\n"
        "# 127.0.1.1\texample-only\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hn, "Path", lambda p: hosts if p == "/etc/hosts" else Path(p))
    hn._patch_etc_hosts("freshbox")
    content = hosts.read_text(encoding="utf-8")
    # Commented row preserved verbatim
    assert "# 127.0.1.1\texample-only" in content
    # New row appended since no live 127.0.1.1 was present
    assert "127.0.1.1\tfreshbox" in content


def test_patch_etc_hosts_noop_if_file_missing(tmp_path, monkeypatch) -> None:
    """On a system without /etc/hosts we silently skip the patch rather
    than erroring. Matches the 'non-fatal' handling in apply_hostname."""
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(hn, "Path", lambda p: missing if p == "/etc/hosts" else Path(p))
    hn._patch_etc_hosts("freshbox")  # must not raise
    assert not missing.exists()
