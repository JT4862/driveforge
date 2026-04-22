"""v0.8.0 — drive-class classifier.

`classify()` maps a drive's (model, transport, rotation_rate) triple
to one of four classes used by the workload-ceiling grading rule.
Operator can add overrides via an optional YAML file; fallback for
unclassifiable drives is the safer (tighter-ceiling) consumer tier.
"""

from __future__ import annotations

from pathlib import Path

from driveforge.core.drive_class import classify, display_name


def test_sas_drive_is_enterprise() -> None:
    """SAS transport is enterprise-only in the market (consumer drives
    ship SATA). Short-circuit before model-string matching."""
    assert classify(model="Any Random Model", transport="sas", rotation_rate=10000) == "enterprise_hdd"
    assert classify(model="Any Random SSD", transport="sas", rotation_rate=0) == "enterprise_ssd"


def test_10k_rpm_hdd_is_enterprise() -> None:
    """10K+ RPM HDDs have no consumer market (Savvio, Cheetah, etc.),
    even if the model prefix isn't in the baked-in table."""
    result = classify(model="Unknown Vendor 10K", transport="sata", rotation_rate=10000)
    assert result == "enterprise_hdd"
    result = classify(model="Some 15K Drive", transport="sata", rotation_rate=15000)
    assert result == "enterprise_hdd"


def test_7200_rpm_sata_depends_on_model() -> None:
    """7200 RPM SATA splits — consumer desktop vs NAS-pro vs enterprise
    Exos. Classifier defers to model prefix at this RPM band."""
    assert classify(
        model="WD Red Pro 4TB", transport="sata", rotation_rate=7200
    ) == "enterprise_hdd"
    assert classify(
        model="WD Blue 4TB", transport="sata", rotation_rate=7200
    ) == "consumer_hdd"
    assert classify(
        model="ST4000DM004-2CV104", transport="sata", rotation_rate=7200
    ) == "consumer_hdd"
    # Exos: smartctl reports marketing line as part of the model string
    # for most variants. Match on the "Exos" prefix in the enterprise
    # table.
    assert classify(
        model="Exos 7E8 ST8000NM0055", transport="sata", rotation_rate=7200
    ) == "enterprise_hdd"


def test_intel_dc_ssd_classifies_enterprise() -> None:
    """Intel DC-series SSDs have enterprise workload ratings. The R720
    boot drive's SSDSC2BB prefix specifically gets enterprise-SSD."""
    result = classify(
        model="INTEL SSDSC2BB120G4",
        transport="sata",
        rotation_rate=0,
    )
    assert result == "enterprise_ssd"


def test_samsung_consumer_ssd_classifies_consumer() -> None:
    """Samsung 8xx/9xx EVO/PRO are consumer-tier."""
    assert classify(
        model="Samsung SSD 860 EVO 500GB",
        transport="sata",
        rotation_rate=0,
    ) == "consumer_ssd"
    assert classify(
        model="Samsung SSD 970 EVO Plus 1TB",
        transport="nvme",
        rotation_rate=0,
    ) == "consumer_ssd"


def test_longest_prefix_match_prefers_enterprise_sub_line() -> None:
    """'WD Red Pro' (enterprise) must beat 'WD Red' (consumer). Both
    prefixes are in the tables; longest-match wins."""
    assert classify(
        model="WD Red Pro 12TB WD121KFBX",
        transport="sata",
        rotation_rate=7200,
    ) == "enterprise_hdd"
    assert classify(
        model="WD Red 4TB WD40EFRX",
        transport="sata",
        rotation_rate=5400,
    ) == "consumer_hdd"


def test_fallback_to_consumer_for_unknown_model() -> None:
    """Ambiguous model + no special transport/RPM signal → default to
    consumer tier. Safer default (tighter workload ceiling)."""
    assert classify(
        model="Mystery Brand Q-42",
        transport="sata",
        rotation_rate=7200,
    ) == "consumer_hdd"
    assert classify(
        model="Mystery Brand Q-42",
        transport="sata",
        rotation_rate=0,
    ) == "consumer_ssd"


def test_operator_override_takes_precedence(tmp_path: Path) -> None:
    """`overrides_path` points at a YAML of model-prefix → class. Any
    match there wins over the baked-in classifier — lets operators
    correct drives we don't recognize."""
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(
        "Mystery Brand Q-42: enterprise_ssd\n"
        "WD Red: enterprise_hdd\n"  # Override — normally consumer_hdd
    )
    # Unknown drive now enterprise_ssd per override
    assert classify(
        model="Mystery Brand Q-42 256GB",
        transport="sata",
        rotation_rate=0,
        overrides_path=overrides,
    ) == "enterprise_ssd"
    # WD Red is normally consumer_hdd, but override bumps it
    assert classify(
        model="WD Red 4TB WD40EFRX",
        transport="sata",
        rotation_rate=5400,
        overrides_path=overrides,
    ) == "enterprise_hdd"


def test_missing_overrides_file_falls_through_cleanly(tmp_path: Path) -> None:
    """If the overrides path doesn't exist (common — file is optional),
    classifier falls through to built-ins without raising."""
    missing = tmp_path / "does-not-exist.yaml"
    assert classify(
        model="Samsung SSD 860 EVO",
        transport="sata",
        rotation_rate=0,
        overrides_path=missing,
    ) == "consumer_ssd"


def test_malformed_overrides_file_falls_through_with_warning(tmp_path: Path, caplog) -> None:
    """A bad YAML file shouldn't crash classification — log + fall through."""
    import logging
    bad = tmp_path / "bad.yaml"
    bad.write_text("this: is: : invalid yaml:::")
    caplog.set_level(logging.WARNING, logger="driveforge.core.drive_class")
    result = classify(
        model="Samsung SSD 860 EVO",
        transport="sata",
        rotation_rate=0,
        overrides_path=bad,
    )
    assert result == "consumer_ssd"
    # A warning should have been logged
    assert any("overrides" in rec.message.lower() for rec in caplog.records)


def test_overrides_reject_invalid_class_values(tmp_path: Path) -> None:
    """Overrides YAML with a bad class value (typo, made-up) is
    silently ignored for that entry — prevents operator typos from
    producing garbage drive_class strings that don't match the
    rated-TBW map."""
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(
        "Samsung: not_a_real_class\n"
        "WD Red: enterprise_hdd\n"  # valid override still honored
    )
    # Samsung override has bogus value → falls through to built-in consumer
    assert classify(
        model="Samsung SSD 860 EVO",
        transport="sata",
        rotation_rate=0,
        overrides_path=overrides,
    ) == "consumer_ssd"
    # WD Red's valid override still applies
    assert classify(
        model="WD Red 4TB",
        transport="sata",
        rotation_rate=5400,
        overrides_path=overrides,
    ) == "enterprise_hdd"


def test_display_name_formats_each_class() -> None:
    assert display_name("enterprise_hdd") == "Enterprise HDD"
    assert display_name("enterprise_ssd") == "Enterprise SSD"
    assert display_name("consumer_hdd") == "Consumer HDD"
    assert display_name("consumer_ssd") == "Consumer SSD"
