"""smartctl wrapper + SMART JSON parser.

Uses `smartctl --json --all /dev/sdX` (smartmontools 7.0+) so we don't
parse English text. Returns a structured `SmartSnapshot`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from driveforge.core.process import run, run_async


class SmartAttribute(BaseModel):
    id: int
    name: str
    value: int | None = None
    worst: int | None = None
    threshold: int | None = None
    raw_value: int | None = None


class SelfTestEntry(BaseModel):
    """v1.0.1+ — one row from the drive's SMART self-test log.

    The log is a ~21-entry ring buffer the drive maintains in firmware,
    written every time the operator (or a host script) issues
    `smartctl --test=short|long|conveyance`. Pre-v1.0.1 DriveForge
    only kept the count + a "did any fail" boolean; v1.0.1's grading
    rules need the per-entry shape to distinguish short-test
    electronics-class failures from long-test media-class failures
    AND distinguish ancient one-offs from recent clusters.
    """
    test_type: str  # "Short" | "Extended" | "Conveyance" | etc.
    passed: bool
    lifetime_hours: int | None = None
    # Where on the drive the failure was first detected (LBA address);
    # None for tests that passed or for SCSI drives whose log doesn't
    # carry the field. Useful for the operator's drive-detail page
    # Test History — clusters of failures at similar LBAs suggest
    # localized media damage.
    lba_first_error: int | None = None
    # If the test was aborted mid-run, what percent of the LBA range
    # was still untested. 0 = test ran to completion; 90 = drive
    # gave up at 10% in. Helps distinguish "drive aborted because of
    # power loss" (high remaining_percent, often safe to ignore)
    # from "drive aborted because it hit unrecoverable media"
    # (typically low remaining_percent at point of failure).
    remaining_percent: int | None = None


class SmartSnapshot(BaseModel):
    """Point-in-time SMART snapshot for a drive.

    Stored pre- and post-test; diffed in Phase 8 to grade degradation.

    v0.8.0+: gained lifetime I/O + wear + error-class fields drawn
    from per-transport sources (NVMe health log / SCSI error-counter
    log / SATA attributes 184/188/196/199/241/242/233/177/169/231).
    These feed the buyer-transparency report on the drive-detail
    page and the new ceiling-based grading rules. Every new field
    is `| None` so drives that don't report the attribute don't get
    graded on a missing signal.
    """

    device: str
    captured_at: datetime
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    power_on_hours: int | None = None
    temperature_c: int | None = None
    reallocated_sectors: int | None = None
    current_pending_sector: int | None = None
    offline_uncorrectable: int | None = None
    udma_crc_error_count: int | None = None
    smart_status_passed: bool | None = None
    attributes: list[SmartAttribute] = []
    raw: dict[str, Any] = {}

    # v0.8.0+ lifetime I/O counters. Bytes (decimal), not LBAs — callers
    # shouldn't have to know the drive's logical block size to interpret.
    # Populated from NVMe data_units_{read,written}, SCSI error-counter-log
    # bytes_processed, or SATA attrs 241/242 × logical_block_size.
    lifetime_host_reads_bytes: int | None = None
    lifetime_host_writes_bytes: int | None = None

    # v0.8.0+ SSD wear signals. `wear_pct_used` is 0-100 (0=new, 100=EOL).
    # Sourced from NVMe percentage_used (direct), or SATA normalized-
    # remaining attrs (233/177/231/169) as `100 - remaining`. None on HDDs.
    wear_pct_used: int | None = None
    # NVMe-only (SATA doesn't have an equivalent). 0-100; when this drops
    # below the drive's advertised available_spare_threshold, the drive
    # is warning us that its error recovery is nearly exhausted.
    available_spare_pct: int | None = None
    available_spare_threshold_pct: int | None = None

    # v0.8.0+ error-class signals. All "counts" are lifetime cumulative
    # unless noted; zero means "drive has never reported one."
    # SATA attr 184 — internal integrity check failure; ANY value > 0 is
    # a hard signal of silent data corruption detected by the drive.
    end_to_end_error_count: int | None = None
    # SATA attr 188 — times a command failed to complete within the
    # kernel's timeout window. High counts indicate unreliable response.
    command_timeout_count: int | None = None
    # SATA attr 196 — count of distinct reallocation EVENTS (vs attr 5
    # which counts total reallocated sectors). Useful for "did this drive
    # just remap something" vs "drive remapped long ago and is now stable".
    reallocation_event_count: int | None = None

    # NVMe-specific fields (all None on SATA/SAS).
    # critical_warning is a bitfield the drive firmware uses to alert
    # the host to conditions requiring operator attention: spare-below-
    # threshold, temperature, reliability degraded, read-only, volatile-
    # memory-backup-failed. ANY non-zero value is an auto-fail signal.
    nvme_critical_warning: int | None = None
    # Count of uncorrected read/write errors reported by the drive's own
    # media layer. Zero-tolerance ceiling-C signal on NVMe.
    nvme_media_errors: int | None = None
    # Power-loss events mid-write. Not a grading signal; informational.
    nvme_unsafe_shutdowns: int | None = None

    # v0.8.0+ self-test log summary (parsed from
    # `ata_smart_self_test_log.standard.table` or its SCSI equivalent).
    # Records whether ANY past self-test failed; the date/LBA of the
    # most recent failure; and total completed tests. Used by the
    # drive-detail page's Test History section and (pre-v1.0.1) as
    # a single blunt ceiling-C grading signal ("drive has a failed
    # test in its past, demote regardless of when / what type").
    self_test_total_count: int | None = None
    self_test_last_failed_at_hour: int | None = None  # POH of last-failed, for trend display
    self_test_has_past_failure: bool | None = None

    # v1.0.1+ per-entry breakdown of the SMART self-test log. Lets
    # grading distinguish "ancient short-test failure followed by 30
    # clean long tests" (low signal) from "long-test failure last
    # week" (high signal). JT's first 15-drive 6TB enterprise pull
    # batch capped 12/14 at C from the single pre-v1.0.1 rule —
    # exactly the over-firing this nuanced version fixes. Optional
    # for backwards compat with rows captured pre-v1.0.1; grading
    # falls back to the summary fields above when the per-entry
    # list is absent.
    self_test_entries: list["SelfTestEntry"] | None = None


ATTR_REALLOCATED = 5
ATTR_POWER_ON_HOURS = 9
# v0.8.0+ error-class attributes
ATTR_END_TO_END_ERROR = 184
ATTR_COMMAND_TIMEOUT = 188
ATTR_TEMP_AIRFLOW = 190
ATTR_TEMP_DRIVE = 194
ATTR_REALLOC_EVENT_COUNT = 196
ATTR_CURRENT_PENDING = 197
ATTR_OFFLINE_UNCORRECTABLE = 198
ATTR_UDMA_CRC_ERROR = 199
# v0.8.0+ lifetime I/O (LBAs; multiply by logical block size for bytes)
ATTR_TOTAL_LBAS_WRITTEN = 241
ATTR_TOTAL_LBAS_READ = 242
# v0.8.0+ SSD wear indicators — vendor-specific, we try each in order.
# Each reports a normalized value (100 → 0) in the `value` field of
# the attribute, representing REMAINING life; wear_pct_used = 100 - value.
SSD_WEAR_ATTR_CANDIDATES = (
    233,  # Intel — Media_Wearout_Indicator
    177,  # Samsung — Wear_Leveling_Count
    231,  # Various — SSD_Life_Left
    169,  # Crucial / Micron — Remaining_Lifetime_Perc
)


def _raw_of(attrs: list[SmartAttribute], attr_id: int) -> int | None:
    for a in attrs:
        if a.id == attr_id:
            return a.raw_value
    return None


def _normalized_of(attrs: list[SmartAttribute], attr_id: int) -> int | None:
    """Return the `value` (normalized, 0-255 typically) field of an
    attribute, distinct from its raw vendor-packed integer.

    SSD wear indicators use the NORMALIZED field (where 100 = factory,
    declining to 0 as wear accumulates) rather than the raw field, which
    varies per vendor. Introduced v0.8.0 alongside wear_pct_used parsing.
    """
    for a in attrs:
        if a.id == attr_id:
            return a.value
    return None


# v0.8.0+ — NVMe `data_units_*` counters are documented in the spec as
# "1000 * 512-byte units" (i.e. 512,000 bytes per unit, NOT 1 MB). This
# is the single most-often-misread field in the NVMe SMART log; third-
# party tools regularly report NVMe lifetime I/O as 2× reality because
# they assume 1 MiB. Keep the constant named so nobody has to rediscover
# this from the spec.
NVME_DATA_UNIT_BYTES = 512_000


def _parse_lifetime_io_and_wear(
    data: dict[str, Any],
    attrs: list[SmartAttribute],
) -> dict[str, int | None]:
    """Extract lifetime reads/writes + SSD wear + NVMe spare from the
    smartctl JSON. Transport-aware: tries NVMe → SAS → SATA in that
    order and returns whichever source has data. Fields that aren't
    reported by the drive's transport come back None.

    Why the three-path structure: smartctl presents each transport's
    data in its own top-level subtree, and the fields that carry
    lifetime I/O only overlap conceptually — not structurally. A
    unified `smartctl --json` schema across transports would be
    lovely, but until then this is the contract we work with.
    """
    out: dict[str, int | None] = {
        "lifetime_host_reads_bytes": None,
        "lifetime_host_writes_bytes": None,
        "wear_pct_used": None,
        "available_spare_pct": None,
        "available_spare_threshold_pct": None,
        "nvme_critical_warning": None,
        "nvme_media_errors": None,
        "nvme_unsafe_shutdowns": None,
    }

    # --- NVMe path: cleanest, universal for NVMe drives
    nvme = data.get("nvme_smart_health_information_log") or {}
    if nvme:
        r = nvme.get("data_units_read")
        w = nvme.get("data_units_written")
        if r is not None:
            out["lifetime_host_reads_bytes"] = r * NVME_DATA_UNIT_BYTES
        if w is not None:
            out["lifetime_host_writes_bytes"] = w * NVME_DATA_UNIT_BYTES
        out["wear_pct_used"] = nvme.get("percentage_used")
        out["available_spare_pct"] = nvme.get("available_spare")
        out["available_spare_threshold_pct"] = nvme.get("available_spare_threshold")
        out["nvme_critical_warning"] = nvme.get("critical_warning")
        out["nvme_media_errors"] = nvme.get("media_errors")
        out["nvme_unsafe_shutdowns"] = nvme.get("unsafe_shutdowns")
        return out

    # --- SAS path: SCSI error-counter-log, bytes native
    scsi_log = data.get("scsi_error_counter_log") or {}
    if scsi_log:
        read_bytes = (scsi_log.get("read") or {}).get("bytes_processed")
        write_bytes = (scsi_log.get("write") or {}).get("bytes_processed")
        if read_bytes is not None:
            out["lifetime_host_reads_bytes"] = int(read_bytes)
        if write_bytes is not None:
            out["lifetime_host_writes_bytes"] = int(write_bytes)
        # Wear % + NVMe-only fields stay None on SAS — SAS drives can
        # report SSD wear via log page 0x11 but smartctl exposes it
        # inconsistently; we skip rather than parse unreliably.
        return out

    # --- SATA path: attrs 241/242 × logical block size, plus wear
    # attributes. Fall through to this when neither NVMe nor SAS
    # structures are present (i.e. a SATA drive).
    sector_size = data.get("logical_block_size", 512) or 512
    writes_lba = _raw_of(attrs, ATTR_TOTAL_LBAS_WRITTEN)
    reads_lba = _raw_of(attrs, ATTR_TOTAL_LBAS_READ)
    if writes_lba is not None:
        out["lifetime_host_writes_bytes"] = writes_lba * sector_size
    if reads_lba is not None:
        out["lifetime_host_reads_bytes"] = reads_lba * sector_size
    # SSD wear via one of the vendor attributes. First one that's
    # present wins; semantics identical (value is normalized REMAINING
    # life, 100 → 0).
    for attr_id in SSD_WEAR_ATTR_CANDIDATES:
        remaining = _normalized_of(attrs, attr_id)
        if remaining is not None and 0 <= remaining <= 100:
            out["wear_pct_used"] = 100 - remaining
            break
    return out


def _parse_self_test_log(data: dict[str, Any]) -> dict[str, Any]:
    """Summarize the ATA/SCSI self-test log into three flat fields for
    the SmartSnapshot: total count, whether any past run failed, and
    the POH at which the last-failed test ran. Doesn't return full
    history — just the signal grading + the drive-detail-page Test
    History section need.

    ATA path reads `ata_smart_self_test_log.standard.table[]`, each
    entry carrying `status.passed` (bool), `type.string` ("Short" /
    "Extended" / etc), and `lifetime_hours`. SCSI path lives under
    `scsi_self_test_0` with slightly different shape; we check both.
    """
    out: dict[str, Any] = {
        "self_test_total_count": None,
        "self_test_last_failed_at_hour": None,
        "self_test_has_past_failure": None,
        # v1.0.1+ per-entry list. None = log section absent (pre-test
        # drive or smartctl couldn't read it); empty list = log section
        # present but no entries yet.
        "self_test_entries": None,
    }
    # ATA self-test log
    ata_tests = (
        (data.get("ata_smart_self_test_log") or {})
        .get("standard", {})
        .get("table", [])
    )
    if ata_tests:
        out["self_test_total_count"] = len(ata_tests)
        failures = [
            t for t in ata_tests
            if not ((t.get("status") or {}).get("passed", True))
        ]
        out["self_test_has_past_failure"] = len(failures) > 0
        if failures:
            # Table is in reverse-chronological order per spec; first
            # failure in iteration = most-recent failure.
            out["self_test_last_failed_at_hour"] = failures[0].get("lifetime_hours")
        # v1.0.1+ per-entry breakdown. Defensive about missing fields —
        # different smartctl versions / drive vendors structure the
        # JSON slightly differently and we'd rather omit a field than
        # crash on a None.
        entries: list[dict[str, Any]] = []
        for t in ata_tests:
            status = t.get("status") or {}
            type_obj = t.get("type") or {}
            entries.append({
                "test_type": (type_obj.get("string") or "Unknown").strip(),
                "passed": bool(status.get("passed", True)),
                "lifetime_hours": t.get("lifetime_hours"),
                "lba_first_error": t.get("lba"),
                "remaining_percent": (
                    status.get("remaining_percent")
                    if status.get("remaining_percent") is not None
                    else status.get("value")  # some smartctl JSONs nest it
                ),
            })
        out["self_test_entries"] = entries
        return out

    # SCSI path (parsed similarly)
    scsi_entries = (data.get("scsi_self_test_0") or {}).get("result", {})
    if scsi_entries:
        # smartctl exposes SCSI self-test as a single most-recent entry;
        # richer history requires log-page-parsing that the library
        # doesn't expose cleanly. Count what we have.
        out["self_test_total_count"] = 1
        passed = scsi_entries.get("string", "").lower().startswith("background scan")
        out["self_test_has_past_failure"] = not passed
        out["self_test_last_failed_at_hour"] = scsi_entries.get("power_on_time", {}).get("hours") if not passed else None
    return out


def parse(payload: str, *, device: str = "") -> SmartSnapshot:
    """Parse `smartctl --json --all` output."""
    data = json.loads(payload)
    attrs: list[SmartAttribute] = []
    for raw in data.get("ata_smart_attributes", {}).get("table", []) or []:
        attrs.append(
            SmartAttribute(
                id=raw.get("id", 0),
                name=raw.get("name", ""),
                value=raw.get("value"),
                worst=raw.get("worst"),
                threshold=raw.get("thresh"),
                raw_value=(raw.get("raw") or {}).get("value"),
            )
        )

    # Temperature: prefer smartctl's pre-decoded `temperature.current` (it
    # knows the per-vendor raw packing — Seagate, for instance, crams
    # current/min/max into a 48-bit int, so reading `raw_value` directly
    # yielded values like 77_309_411_358 for a drive actually running at
    # 30 °C, which then tripped the thermal-excursion grade-C demotion on
    # healthy drives). Only fall back to attribute raw values when the
    # top-level field is absent AND the raw passes a 0-150 °C sanity check;
    # if the raw looks packed (>= 150), pick its low-16-bit lane, which is
    # where every Seagate/HGST/WD drive we've seen puts the current temp.
    temp: int | None = None
    top_level = (data.get("temperature") or {}).get("current")
    if isinstance(top_level, int) and 0 < top_level < 150:
        temp = top_level
    else:
        for attr_id in (ATTR_TEMP_DRIVE, ATTR_TEMP_AIRFLOW):
            raw = _raw_of(attrs, attr_id)
            if raw is None:
                continue
            if 0 < raw < 150:
                temp = raw
                break
            low16 = raw & 0xFFFF
            if 0 < low16 < 150:
                temp = low16
                break

    # NVMe health log sits at `nvme_smart_health_information_log`
    nvme_log = data.get("nvme_smart_health_information_log") or {}
    power_on_hours = (
        _raw_of(attrs, ATTR_POWER_ON_HOURS)
        or nvme_log.get("power_on_hours")
        or (data.get("power_on_time") or {}).get("hours")
    )

    status = data.get("smart_status") or {}

    # v0.8.0+: transport-aware lifetime I/O + wear + NVMe-health + self-test.
    # Each helper returns a dict of fields that are either populated (for
    # drives/transports that report the signal) or None. Merged into the
    # SmartSnapshot construction below.
    io_wear = _parse_lifetime_io_and_wear(data, attrs)
    test_log = _parse_self_test_log(data)

    return SmartSnapshot(
        device=device or (data.get("device") or {}).get("name", ""),
        captured_at=datetime.now(UTC),
        model=data.get("model_name"),
        serial=data.get("serial_number"),
        firmware=data.get("firmware_version"),
        power_on_hours=power_on_hours,
        temperature_c=temp,
        reallocated_sectors=_raw_of(attrs, ATTR_REALLOCATED),
        current_pending_sector=_raw_of(attrs, ATTR_CURRENT_PENDING),
        offline_uncorrectable=_raw_of(attrs, ATTR_OFFLINE_UNCORRECTABLE),
        udma_crc_error_count=_raw_of(attrs, ATTR_UDMA_CRC_ERROR),
        smart_status_passed=status.get("passed"),
        attributes=attrs,
        raw=data,
        # v0.8.0+ new error-class fields straight from SATA attrs
        end_to_end_error_count=_raw_of(attrs, ATTR_END_TO_END_ERROR),
        command_timeout_count=_raw_of(attrs, ATTR_COMMAND_TIMEOUT),
        reallocation_event_count=_raw_of(attrs, ATTR_REALLOC_EVENT_COUNT),
        # v0.8.0+ fields from the transport-aware helper
        lifetime_host_reads_bytes=io_wear["lifetime_host_reads_bytes"],
        lifetime_host_writes_bytes=io_wear["lifetime_host_writes_bytes"],
        wear_pct_used=io_wear["wear_pct_used"],
        available_spare_pct=io_wear["available_spare_pct"],
        available_spare_threshold_pct=io_wear["available_spare_threshold_pct"],
        nvme_critical_warning=io_wear["nvme_critical_warning"],
        nvme_media_errors=io_wear["nvme_media_errors"],
        nvme_unsafe_shutdowns=io_wear["nvme_unsafe_shutdowns"],
        # v0.8.0+ self-test history summary
        self_test_total_count=test_log["self_test_total_count"],
        self_test_last_failed_at_hour=test_log["self_test_last_failed_at_hour"],
        self_test_has_past_failure=test_log["self_test_has_past_failure"],
        # v1.0.1+ per-entry breakdown — feeds the nuanced grading
        # rules in core/grading.py and the drive-detail Test History
        # render.
        self_test_entries=(
            [SelfTestEntry(**e) for e in test_log["self_test_entries"]]
            if test_log.get("self_test_entries") is not None
            else None
        ),
    )


# Per-call timeouts stop a hung drive from pinning the daemon's worker thread
# in D-state. The old Seagate ST300MM0006 on this R720 demonstrated the
# failure mode: firmware stopped responding, every smartctl piled up for the
# 180s kernel SCSI timeout, dashboard rendering hung. Keep these short enough
# that a failing drive gets flagged as dead rather than hanging the UI.
SMARTCTL_INFO_TIMEOUT = 30.0
SMARTCTL_TEST_START_TIMEOUT = 30.0
SMARTCTL_TEST_STATUS_TIMEOUT = 15.0


def snapshot(device: str, *, timeout: float = SMARTCTL_INFO_TIMEOUT) -> SmartSnapshot:
    """Take a SMART snapshot of a device.

    Raises `subprocess.TimeoutExpired` if smartctl hangs past `timeout` — the
    caller should treat that as drive-dead rather than waiting forever.

    Sync variant — used by CLI entrypoints, tests, and non-async callers.
    From an async context, prefer `snapshot_async` (v0.6.9+) to avoid
    burning a drive-command-executor thread per call.
    """
    result = run(["smartctl", "--json", "--all", device], timeout=timeout)
    # smartctl returns non-zero for minor warnings we still want to parse
    if not result.stdout:
        raise RuntimeError(f"smartctl returned no output for {device}: {result.stderr}")
    return parse(result.stdout, device=device)


async def snapshot_async(
    device: str,
    *,
    timeout: float = SMARTCTL_INFO_TIMEOUT,
) -> SmartSnapshot:
    """Async variant of `snapshot` (v0.6.9+).

    Spawns smartctl via `asyncio.create_subprocess_exec` instead of
    burning a thread in the drive-command executor. Preferred from
    async code paths (orchestrator, telemetry sampler, auto-print).

    Semantics match `snapshot`:
      - Raises `asyncio.TimeoutError` on hang past `timeout`. Callers
        that previously caught `subprocess.TimeoutExpired` need to
        widen the except (both are raised by run_async's timeout
        path — TimeoutError from asyncio.wait_for, TimeoutExpired
        never from this code path).
      - Returns a `SmartSnapshot` on success (even on non-zero rc —
        smartctl reports warnings that way).
      - Raises `RuntimeError` on empty stdout.

    Parse is pure-Python and fast, so it runs inline on the event
    loop thread; no need to offload.
    """
    result = await run_async(
        ["smartctl", "--json", "--all", device],
        timeout=timeout,
    )
    if not result.stdout:
        raise RuntimeError(f"smartctl returned no output for {device}: {result.stderr}")
    return parse(result.stdout, device=device)


def start_self_test(
    device: str,
    *,
    kind: str = "short",
    timeout: float = SMARTCTL_TEST_START_TIMEOUT,
) -> None:
    """Start a SMART self-test. `kind` ∈ {'short', 'long'}."""
    if kind not in {"short", "long"}:
        raise ValueError(f"unsupported self-test kind: {kind}")
    run(["smartctl", "--test", kind, device], check=True, timeout=timeout)


class SelfTestStatus(BaseModel):
    in_progress: bool
    percent_complete: int | None = None  # 0-100, None if not in progress
    last_result_passed: bool | None = None  # None if no test has completed
    status_string: str = ""


def self_test_status(
    device: str,
    *,
    timeout: float = SMARTCTL_TEST_STATUS_TIMEOUT,
) -> SelfTestStatus:
    """Query SMART self-test progress via smartctl (sync wrapper)."""
    result = run(["smartctl", "--json", "-c", "-l", "selftest", device], timeout=timeout)
    if not result.stdout:
        return SelfTestStatus(in_progress=False)
    return parse_self_test_status(result.stdout)


def parse_self_test_status(payload: str) -> SelfTestStatus:
    """Pure-function parser for `smartctl --json -c -l selftest` output.

    Handles ATA (`ata_smart_data.self_test`), NVMe
    (`nvme_self_test_log.current_self_test_operation`), and SAS
    (`scsi_self_test_N.result`) log shapes. Returns in-progress + pass/fail.
    """
    import json as _json

    try:
        data = _json.loads(payload)
    except _json.JSONDecodeError:
        return SelfTestStatus(in_progress=False)
    # ATA path: ata_smart_data.self_test.status
    ata = (data.get("ata_smart_data") or {}).get("self_test") or {}
    status = ata.get("status") or {}
    remaining = status.get("remaining_percent")
    value = status.get("value")
    if remaining is not None:
        return SelfTestStatus(
            in_progress=True,
            percent_complete=100 - int(remaining),
            status_string=status.get("string", ""),
        )
    # ATA status values >= 0x80 mean in-progress; < 0x80 means completed
    if isinstance(value, int) and value >= 0x80:
        # Low nibble × 10 = percent remaining
        pct_remaining = (value & 0x0F) * 10
        return SelfTestStatus(
            in_progress=True,
            percent_complete=100 - pct_remaining,
            status_string=status.get("string", ""),
        )
    # Not in progress; was the last completed test a pass?
    last_passed: bool | None = None
    # ATA self-test log
    last_log = (data.get("ata_smart_self_test_log") or {}).get("standard") or {}
    table = last_log.get("table") or []
    if table:
        top = table[0]
        st = (top.get("status") or {}).get("string", "").lower()
        if "without error" in st or "completed without" in st:
            last_passed = True
        elif any(word in st for word in ("fail", "error", "aborted")):
            last_passed = False
    # SCSI / SAS self-test log. smartctl --json emits scsi_self_test_0
    # through _19 with this shape:
    #   {"code": {...}, "result": {"value": N, "string": "..."}, ...}
    # result.value semantics per SPC-4 / smartmontools:
    #   0 = Completed without error
    #   1 = Aborted by user (SEND DIAGNOSTIC)
    #   2 = Aborted by reset or power cycle
    #   3 = Unknown error
    #   4-8 = Various failure segments
    #   15 = Self-test in progress
    # (The first entry with result.value=15 indicates a running test.)
    if last_passed is None:
        for i in range(20):
            entry = data.get(f"scsi_self_test_{i}")
            if not entry:
                continue
            result = entry.get("result") or {}
            result_val = result.get("value")
            if result_val == 15:
                return SelfTestStatus(
                    in_progress=True,
                    percent_complete=None,  # SAS log doesn't expose progress %
                    status_string=result.get("string", ""),
                )
            if isinstance(result_val, int):
                last_passed = result_val == 0
                break  # Most recent completed entry is authoritative
    # NVMe path: self_test_log.current_self_test_operation
    nvme = (data.get("nvme_self_test_log") or {}).get("current_self_test_operation") or {}
    nvme_op_value = nvme.get("value")
    if isinstance(nvme_op_value, int) and nvme_op_value != 0:
        nvme_completion = data.get("nvme_self_test_log", {}).get("current_self_test_completion") or {}
        nvme_pct = nvme_completion.get("percent_remaining")
        return SelfTestStatus(
            in_progress=True,
            percent_complete=(100 - int(nvme_pct)) if nvme_pct is not None else None,
            status_string=nvme.get("string", ""),
        )
    return SelfTestStatus(
        in_progress=False,
        last_result_passed=last_passed,
        status_string=status.get("string", "") if isinstance(status, dict) else "",
    )
