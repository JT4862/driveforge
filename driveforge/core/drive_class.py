"""Drive-class classifier: {enterprise, consumer} × {hdd, ssd}.

v0.8.0+. Feeds the workload-ceiling grading rule (which needs a rated
TBW to compare lifetime_host_writes_bytes against) and the buyer-
transparency report (so the printed sheet can honestly label the
class the drive was graded under).

No clean SMART attribute reports "enterprise vs consumer" — that's a
marketing categorization, not a technical one. We use three proxies:

  1. Transport + rotation_rate — SAS is enterprise territory (consumer
     drives don't ship SAS); 10K/15K RPM HDDs are enterprise by
     definition. 7200 RPM drives split between classes; 5400 RPM is
     consumer/archive.
  2. Model-string prefix patterns — WD's `WUH`, `HUS`, `Ultrastar`
     prefixes are enterprise; `Red Pro`, `Purple Pro`, `Gold` are
     enterprise-tier consumer; `Blue`, `Green`, `Red` (non-Pro) are
     consumer. Seagate's `Exos`, `IronWolf Pro`, `Nytro`, `SkyHawk AI`
     are enterprise; `BarraCuda`, `FireCuda`, `IronWolf` are consumer.
     Intel's DC-series SSDs (`DC`, `D3`, `D5`, `D7`, `SSDSC2BB`) are
     enterprise; consumer Intel (`SSDSC2KB`, `600p`, `660p`) are
     consumer. Samsung's `PM`/`SM` prefixes are OEM/enterprise;
     `860 EVO`, `970 EVO`, etc. are consumer.
  3. Operator override — optional `/etc/driveforge/drive_class_overrides.yaml`
     keyed on model prefix. Takes precedence over the built-in rules.
     Lets operators tune for drives not in the baked-in table without
     needing a code change.

Fallback on ambiguous drives: `consumer` (the safer / stricter end —
consumer rated-TBW is lower, so an ambiguous drive gets the tighter
workload ceiling by default). Operators can override per-model.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


DriveClass = Literal["enterprise_hdd", "enterprise_ssd", "consumer_hdd", "consumer_ssd"]


# Model-prefix → class table. Order matters for longest-prefix matching:
# "WD Red Pro" must match before "WD Red" to classify Red Pro as
# enterprise. We handle that by checking longer prefixes first via
# sorted-by-length iteration in `classify_by_model()`.
_ENTERPRISE_HDD_PREFIXES = (
    # Western Digital enterprise
    "WUH",  # Ultrastar DC
    "HUS",  # Ultrastar DC (post-HGST rebrand)
    "HUH",  # Ultrastar helium
    "Ultrastar",
    "WD Red Pro",
    "WD Gold",
    "WD Purple Pro",
    # Seagate enterprise
    "Exos",
    "Constellation",
    "Cheetah",
    "Savvio",
    "IronWolf Pro",
    "Nytro",
    # HGST (pre-WD acquisition)
    "HGST HUS",
    "HGST HUH",
    "HGST HTS72",  # 7200 RPM 2.5" enterprise
    # Toshiba enterprise
    "MG0",
    "MG07",
    "MG08",
    "MG09",
    "AL13",
    "AL14",
    "AL15",  # Toshiba enterprise 2.5"
)
_CONSUMER_HDD_PREFIXES = (
    # Western Digital consumer
    "WD Blue",
    "WD Green",
    "WD Red",  # Red (non-Pro) is NAS-class but not enterprise-rated-workload
    "WD Black",  # gaming/performance but not enterprise workload
    "WD Caviar",
    "WD Elements",
    "WD My",
    # Seagate consumer
    "ST3000DM",  # BarraCuda desktop
    "ST4000DM",
    "ST8000DM",
    "BarraCuda",
    "FireCuda",
    "IronWolf",  # non-Pro
    "SkyHawk",  # surveillance, workload-rated but consumer-class
    # Toshiba consumer
    "DT0",
    "HDWD",
    # HGST 2.5" laptop / consumer
    "HTS545",
    "HTS547",
    "HTS725",  # Travelstar 7200 consumer 2.5"
)
_ENTERPRISE_SSD_PREFIXES = (
    # Intel DC
    "INTEL SSDSC2BB",  # DC S3500 / S3510 / S3520 etc — the Intel boot drives
    "INTEL SSDSC2BA",  # DC S3700
    "INTEL SSDSC2KG",  # DC D3-S4510
    "INTEL SSDPE2",  # DC P-series NVMe
    "Intel DC",
    # Samsung enterprise/OEM
    "SAMSUNG MZ",  # OEM/enterprise PM/SM series typically show this prefix
    "Samsung SM",
    "Samsung PM",
    # Micron enterprise
    "Micron_5",  # 5100/5200/5300 enterprise SATA SSD
    "Micron_9",  # 9300 NVMe enterprise
    # Kioxia/Toshiba enterprise
    "KIOXIA KCD",
    "KIOXIA KPM",
    "TOSHIBA KXG",
    # Seagate / WD NVMe enterprise
    "Seagate Nytro",
)
_CONSUMER_SSD_PREFIXES = (
    "Samsung SSD 8",  # 840, 850, 860, 870 EVO / PRO
    "Samsung SSD 9",  # 970 EVO, 980, 990
    "Crucial MX",
    "Crucial BX",
    "Crucial P",
    "WD_BLACK",
    "WDS",  # consumer SATA (WD Blue SATA: WDS100T2B0A etc)
    "Kingston",
    "ADATA",
    "Sandisk",
    "INTEL SSDSC2KB",  # 600p / 660p consumer family
)


# Transport + rotation heuristics — checked before model strings so that
# a SAS 15K drive is enterprise even if its model isn't in the prefix
# table yet.
def _class_from_transport_and_rpm(
    transport: str | None,
    rotation_rate: int | None,
    is_ssd: bool,
) -> DriveClass | None:
    t = (transport or "").lower()
    # SAS drives are universally enterprise — consumer drives ship SATA.
    if t == "sas":
        return "enterprise_ssd" if is_ssd else "enterprise_hdd"
    # 10K+ RPM HDDs are enterprise 2.5" (Savvio, Cheetah, etc.) — no
    # consumer market for those.
    if not is_ssd and rotation_rate is not None and rotation_rate >= 10000:
        return "enterprise_hdd"
    return None


def _longest_prefix_match(model: str, prefixes: tuple[str, ...]) -> bool:
    """Return True iff `model` starts with any entry in `prefixes`,
    checking longer prefixes first. Order matters so "WD Red Pro"
    (enterprise) doesn't get short-circuited by "WD Red" (consumer).
    """
    m = (model or "").strip()
    # Sort by length descending — longest prefix wins.
    for p in sorted(prefixes, key=len, reverse=True):
        if m.startswith(p):
            return True
    return False


def classify(
    *,
    model: str | None,
    transport: str | None,
    rotation_rate: int | None,
    overrides_path: Path | None = None,
) -> DriveClass:
    """Classify a drive by model + transport + rotation rate.

    Returns one of: "enterprise_hdd", "enterprise_ssd", "consumer_hdd",
    "consumer_ssd". Never returns None — ambiguous drives default to
    the consumer class for their media type (the safer default, since
    consumer rated-TBW is lower and ambiguous drives get the tighter
    workload ceiling).

    `overrides_path` points at an optional YAML file of model-prefix →
    class mappings. Operator-configurable. Examples:

        "WD Red Plus": consumer_hdd
        "HGST HTS725": consumer_hdd  # laptop drive
        "Custom_OEM_": enterprise_ssd

    The first matching override wins; unmatched models fall through to
    the baked-in classifier.
    """
    is_ssd = rotation_rate is not None and rotation_rate == 0

    # Pass 1: operator overrides (if file present)
    if overrides_path is not None and overrides_path.is_file():
        try:
            import yaml
            with overrides_path.open() as f:
                overrides = yaml.safe_load(f) or {}
            if isinstance(overrides, dict):
                for prefix, cls in overrides.items():
                    if model and model.startswith(prefix) and cls in (
                        "enterprise_hdd", "enterprise_ssd", "consumer_hdd", "consumer_ssd"
                    ):
                        return cls  # type: ignore[return-value]
        except Exception:  # noqa: BLE001
            logger.warning(
                "drive_class overrides at %s failed to parse; falling through",
                overrides_path,
                exc_info=True,
            )

    # Pass 2: transport + RPM heuristics (catch SAS + 10K+ RPM)
    by_xport = _class_from_transport_and_rpm(transport, rotation_rate, is_ssd)
    if by_xport is not None:
        return by_xport

    # Pass 3: model-prefix match against baked-in tables
    if model:
        if _longest_prefix_match(model, _ENTERPRISE_HDD_PREFIXES):
            return "enterprise_hdd"
        if _longest_prefix_match(model, _CONSUMER_HDD_PREFIXES):
            return "consumer_hdd"
        if _longest_prefix_match(model, _ENTERPRISE_SSD_PREFIXES):
            return "enterprise_ssd"
        if _longest_prefix_match(model, _CONSUMER_SSD_PREFIXES):
            return "consumer_ssd"

    # Pass 4: default to consumer for the media type. Safer default
    # (tighter workload ceiling) for drives we can't identify.
    return "consumer_ssd" if is_ssd else "consumer_hdd"


def display_name(cls: DriveClass) -> str:
    """Human-readable class name for the drive-detail page and cert
    rationale. Avoids operator confusion about internal underscore
    snake-case. Example: `enterprise_hdd` → "Enterprise HDD"."""
    return {
        "enterprise_hdd": "Enterprise HDD",
        "enterprise_ssd": "Enterprise SSD",
        "consumer_hdd": "Consumer HDD",
        "consumer_ssd": "Consumer SSD",
    }.get(cls, cls)
