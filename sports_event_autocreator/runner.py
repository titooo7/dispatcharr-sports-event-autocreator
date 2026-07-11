"""
Job orchestration for the Sports Event Auto-Creator plugin.

Runs one "job" (equivalent to one invocation of the original CLI script):
Phase 1 EPG-based search over an XMLTV feed, Phase 2 name-based search over
Dispatcharr streams, then channel cleanup/purge and creation.

All Dispatcharr access goes through the Django ORM (no HTTP, no credentials),
as required by the Dispatcharr plugin guidelines.
"""

import gzip
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import requests

from . import engine

# User-Agent used for outbound HTTP (XMLTV fetch) and as the fallback for
# stream probes when an m3u account has no configured agent.
DEFAULT_USER_AGENT = "TiviMate/5.1.6 (Android 16)"


class JobConfigError(Exception):
    """Raised for invalid job configuration."""


class JobRuntimeError(Exception):
    """Raised when a job cannot run (e.g. XMLTV fetch failure)."""


# ---------------------------------------------------------------------------
# Job configuration
# ---------------------------------------------------------------------------

JOB_DEFAULTS = {
    "enabled": True,
    "epg_sources": [],          # names of Dispatcharr EPG sources (win over xmltv_url)
    "xmltv_url": "",            # optional; Phase 1 skipped when both are empty
    "search": [],               # list of search terms
    "exclude": [],              # list of exclusion terms
    "exclude_stream_prefixes": [],  # drop EPG-match streams whose name starts with these
    "search_descriptions": True,  # also match terms in EPG programme descriptions
    "group": "",                # target channel group name (required)
    "start_number": None,       # starting channel number
    "cleanup": False,           # delete channels matching excludes / too old
    "purge_unmatched": False,   # delete group channels not in current results
    "purge_group": False,       # delete ALL group channels before creating
    "preserve_below": None,     # with purge_group: protect numbers below N
    "preserve_above": None,     # with purge_group: protect numbers above N
    "unassigned": False,        # only use streams not already assigned
    "no_region_label": False,   # hide 🌍 region labels
    "today_only": False,
    "future_only": False,
    "upcoming": False,          # today + upcoming days
    "days": 2,                  # days window for "upcoming"
    "date": "",                 # YYYY-MM-DD filter
    "max_past_hours": None,
    "max_future_hours": None,
    "country_flags": False,
    "pin_top": [],              # pin channels containing these terms to the top
    "split_streams": False,     # one channel per stream instead of bundling
    "max_split": 0,             # cap per-programme channels when splitting
    "require_time": False,      # skip name-based streams without embedded time
    "check_black": False,       # probe EPG-match streams and skip black screens
}

_LIST_KEYS = {"search", "exclude", "exclude_stream_prefixes", "pin_top", "epg_sources"}

# Settings-key prefix for the per-source checkboxes:
#   job:<job-name>:epgsrc:<source-name> -> bool
# The source name is everything after the prefix, so it may contain any
# characters (these keys are only ever constructed and prefix-matched).
EPG_SOURCE_TOGGLE = "epgsrc"


def normalize_job(job: dict, index: int = 0) -> SimpleNamespace:
    """Validate a raw job dict and return a namespace with all defaults filled."""
    if not isinstance(job, dict):
        raise JobConfigError(f"Job #{index + 1} is not a JSON object")

    name = str(job.get("name") or "").strip()
    if not name:
        raise JobConfigError(f"Job #{index + 1} is missing required key \"name\"")
    if ":" in name or "," in name:
        raise JobConfigError(f"Job name '{name}' must not contain ':' or ','")

    # Back-compat: accept the old single-source key as a one-item list.
    job = dict(job)
    if "epg_source" in job and "epg_sources" not in job:
        legacy = str(job.pop("epg_source") or "").strip()
        if legacy and legacy.lower() != EPG_SOURCE_NONE:
            job["epg_sources"] = [legacy]
    else:
        job.pop("epg_source", None)

    unknown = set(job.keys()) - set(JOB_DEFAULTS.keys()) - {"name"}
    if unknown:
        raise JobConfigError(
            f"Job '{name}' has unknown key(s): {', '.join(sorted(unknown))}"
        )

    cfg = dict(JOB_DEFAULTS)
    cfg.update({k: v for k, v in job.items() if k != "name"})
    cfg["name"] = name

    for key in _LIST_KEYS:
        val = cfg[key]
        if val is None:
            cfg[key] = []
        elif isinstance(val, str):
            cfg[key] = [val]
        elif not isinstance(val, list):
            raise JobConfigError(f"Job '{name}': \"{key}\" must be a list of strings")

    if not cfg["group"]:
        raise JobConfigError(f"Job '{name}' is missing required key \"group\"")
    if not cfg["epg_sources"] and not cfg["xmltv_url"] and not cfg["search"]:
        raise JobConfigError(
            f"Job '{name}' needs at least \"epg_sources\", \"xmltv_url\" or \"search\"")
    if cfg["date"]:
        try:
            datetime.strptime(cfg["date"], "%Y-%m-%d")
        except ValueError:
            raise JobConfigError(f"Job '{name}': invalid \"date\" (use YYYY-MM-DD)")

    return SimpleNamespace(**cfg)


def parse_jobs(jobs_json: str) -> List[SimpleNamespace]:
    """Parse and validate a jobs JSON array. Raises JobConfigError."""
    try:
        raw = json.loads(jobs_json or "[]")
    except json.JSONDecodeError as e:
        raise JobConfigError(f"Jobs JSON is not valid JSON: {e}")
    if not isinstance(raw, list):
        raise JobConfigError("Jobs JSON must be a JSON array of job objects")

    jobs = [normalize_job(j, i) for i, j in enumerate(raw)]
    names = [j.name for j in jobs]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise JobConfigError(f"Duplicate job name(s): {', '.join(sorted(dupes))}")
    return jobs


# ---------------------------------------------------------------------------
# Per-job UI settings (flat keys like "job:boxeo:search")
#
# The Dispatcharr plugin UI renders a flat list of fields, so each job is
# exposed as a group of individually-typed fields instead of one JSON blob.
# The specs below drive both the generated UI fields (plugin.py) and the
# settings → job conversion at run time.
# ---------------------------------------------------------------------------

# (key, ui_type, label, help_text). ui_type "lines" = multi-line text,
# one entry per line. "purge_mode" is a UI-only select folded into the
# purge_group/purge_unmatched booleans.
JOB_FIELD_SPECS = [
    ("enabled",          "boolean", "Enabled",
     "Untick to skip this job without deleting its configuration."),
    ("group",            "string",  "Channel group",
     "Target channel group (created automatically if missing). Required."),
    ("xmltv_url",        "string",  "XMLTV URL",
     "Optional. External XMLTV URL (or a file path under /data) for the EPG "
     "search phase. Fetched once per run even if several jobs share it."),
    ("search",           "lines",   "Search terms (one per line)",
     "EPG phase matches whole words in programme title/description; name phase matches substrings in stream names."),
    ("exclude",          "lines",   "Exclude terms (one per line)",
     "Whole-word exclusions applied to programmes, stream names and cleanup."),
    ("exclude_stream_prefixes", "lines", "Exclude stream-name prefixes (EPG matches, one per line)",
     "Candidate streams of an EPG match whose name starts with one of these "
     "prefixes (e.g. 'SKY:', 'PL:', 'RO:') are dropped and the next candidate "
     "is used. Case-insensitive. Only affects the EPG phase — name-search "
     "streams are controlled by the search/exclude terms."),
    ("search_descriptions", "boolean", "Search programme descriptions",
     "Also match search terms in EPG programme descriptions, not just titles. "
     "Disable if rich EPG descriptions cause false matches (e.g. films/series "
     "mentioning a sport). Exclude terms always check descriptions."),
    ("purge_mode",       "select",  "Purge mode",
     "Full purge deletes all group channels before recreating (respects 'preserve below'); "
     "unmatched-only deletes group channels absent from the current results."),
    ("cleanup",          "boolean", "Cleanup old/excluded channels",
     "Delete group channels matching exclude terms or older than 'max past hours'."),
    ("unassigned",       "boolean", "Only unassigned streams",
     "Ignore streams already assigned to a channel."),
    ("start_number",     "number",  "Starting channel number (0 = none)", ""),
    ("preserve_below",   "number",  "Preserve channels numbered below (0 = off)",
     "With full purge: protect manually curated channels below this number."),
    ("preserve_above",   "number",  "Preserve channels numbered above (0 = off)",
     "With full purge: protect manually added channels above this number "
     "(e.g. 24/7 channels placed at the end of the group)."),
    ("upcoming",         "boolean", "Only today + upcoming days", ""),
    ("days",             "number",  "Upcoming window (days)", ""),
    ("max_past_hours",   "number",  "Max past hours (0 = off)",
     "Drop events that started more than this many hours ago."),
    ("max_future_hours", "number",  "Max future hours (0 = off)",
     "Drop events starting further ahead than this."),
    ("country_flags",    "boolean", "Country flag emojis", ""),
    ("no_region_label",  "boolean", "Hide 🌍 region labels", ""),
    ("pin_top",          "lines",   "Pin to top (one per line)",
     "Channels containing these terms are pinned to the top, in the order given."),
    ("split_streams",    "boolean", "One channel per stream",
     "Instead of bundling all streams of an event into one channel with failover."),
    ("max_split",        "number",  "Max channels per event (0 = unlimited)",
     "Only used when 'one channel per stream' is on."),
    ("require_time",     "boolean", "Require embedded date/time",
     "Skip name-based streams whose name has no recognizable DATE (day+month "
     "or a weekday). A bare time like '8:10pm' does not count — it would be "
     "re-read as 'today' forever and keep recreating channels for old events."),
    ("check_black",      "boolean", "Skip black-screen streams (EPG matches)",
     "Probes each candidate stream of an EPG match with ffmpeg (~5s sample, "
     "see the global 'Black-screen probe seconds' setting). Streams showing a "
     "black screen OR failing the probe (HTTP error, timeout, no video) are "
     "skipped and the next candidate is tried — a probed stream must prove it "
     "plays. Name-search matches are never probed."),
]

PURGE_MODE_OPTIONS = [
    {"value": "none", "label": "No purge"},
    {"value": "purge_group", "label": "Full purge (recreate group)"},
    {"value": "purge_unmatched", "label": "Purge unmatched only"},
]

JOBS_LIST_KEY = "jobs_list"

# Sentinel for "no EPG source selected" in the per-job select field.
# Dispatcharr's PluginFieldSerializer rejects select options with a blank
# value (CharField without allow_blank), silently dropping the whole field —
# so the UI value can never be "".
EPG_SOURCE_NONE = "none"


def job_field_id(name: str, key: str) -> str:
    return f"job:{name}:{key}"


def load_seed_jobs() -> Dict[str, dict]:
    """Shipped example jobs (jobs.default.json), keyed by job name."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.default.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {str(j.get("name", "")).strip(): j for j in raw if isinstance(j, dict)}
    except Exception:
        return {}


def parse_job_names(settings: dict, seeds: Optional[Dict[str, dict]] = None) -> List[str]:
    """Job names from the 'jobs_list' setting (falls back to the seed jobs)."""
    raw = settings.get(JOBS_LIST_KEY)
    if raw is None or not str(raw).strip():
        seeds = seeds if seeds is not None else load_seed_jobs()
        return list(seeds.keys())
    names = []
    for part in str(raw).split(","):
        name = part.strip()
        if not name:
            continue
        if ":" in name:
            raise JobConfigError(f"Job name '{name}' must not contain ':'")
        if name not in names:
            names.append(name)
    return names


def _seed_value(seeds: Dict[str, dict], name: str, key: str):
    """Default value for a job field: the shipped seed job, else JOB_DEFAULTS."""
    seed = seeds.get(name, {})
    if key == "purge_mode":
        if seed.get("purge_group"):
            return "purge_group"
        if seed.get("purge_unmatched"):
            return "purge_unmatched"
        return "none"
    if key in seed:
        return seed[key]
    return JOB_DEFAULTS[key]


def job_ui_default(seeds: Dict[str, dict], name: str, key: str):
    """Seed value converted to what the UI field type expects."""
    val = _seed_value(seeds, name, key)
    if key in _LIST_KEYS:
        return "\n".join(val or [])
    if key in ("start_number", "preserve_below", "preserve_above", "max_past_hours", "max_future_hours"):
        return val if val is not None else 0
    return val


def _as_lines(value) -> List[str]:
    if isinstance(value, list):
        items = [str(v).strip() for v in value]
    else:
        items = [line.strip() for line in str(value or "").splitlines()]
    return [i for i in items if i]


def _as_number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def job_from_settings(settings: dict, name: str, seeds: Dict[str, dict]) -> SimpleNamespace:
    """Assemble one job from the flat per-job settings keys."""
    cfg = {"name": name}
    for key, ui_type, _label, _help in JOB_FIELD_SPECS:
        value = settings.get(job_field_id(name, key))
        if value is None:
            value = job_ui_default(seeds, name, key)

        if key == "purge_mode":
            cfg["purge_group"] = value == "purge_group"
            cfg["purge_unmatched"] = value == "purge_unmatched"
        elif ui_type == "lines":
            cfg[key] = _as_lines(value)
        elif key in ("start_number", "preserve_below", "preserve_above", "max_past_hours", "max_future_hours"):
            num = _as_number(value, 0)
            cfg[key] = num if num > 0 else None
        elif key == "days":
            cfg[key] = max(int(_as_number(value, JOB_DEFAULTS["days"])), 1)
        elif key == "max_split":
            cfg[key] = max(int(_as_number(value, 0)), 0)
        elif ui_type == "boolean":
            cfg[key] = bool(value)
        else:
            cfg[key] = str(value or "").strip()

    # EPG sources come from per-source checkboxes (job:<name>:epgsrc:<source>).
    # If no checkbox key exists at all (settings saved before v1.4.0), fall
    # back to the old single-select value; once any checkbox has been saved,
    # the checkboxes are authoritative (all-unticked means "no EPG source").
    toggle_prefix = f"{job_field_id(name, EPG_SOURCE_TOGGLE)}:"
    toggle_keys = [k for k in settings if k.startswith(toggle_prefix)]
    if toggle_keys:
        cfg["epg_sources"] = sorted(k[len(toggle_prefix):] for k in toggle_keys
                                    if settings.get(k))
    else:
        legacy = str(settings.get(job_field_id(name, "epg_source")) or "").strip()
        if legacy and legacy.lower() != EPG_SOURCE_NONE:
            cfg["epg_sources"] = [legacy]
        else:
            cfg["epg_sources"] = list(_seed_value(seeds, name, "epg_sources") or [])

    return normalize_job(cfg)


def job_to_dict(job: SimpleNamespace) -> dict:
    """
    Inverse of normalize_job: a shareable JSON-schema dict (same format as
    jobs.default.json). Keys whose value equals the default are omitted,
    except the essentials, to keep exports compact but self-explanatory.
    """
    always = {"enabled", "group", "xmltv_url", "search", "exclude"}
    out = {"name": job.name}
    for key, default in JOB_DEFAULTS.items():
        val = getattr(job, key)
        if key in always or val != default:
            out[key] = val
    return out


def settings_updates_from_jobs(jobs: List[SimpleNamespace]) -> dict:
    """
    Flat per-job settings keys representing the given jobs — the inverse of
    job_from_settings. Used by the import action to write a shared JSON
    config into the UI-backed settings.
    """
    updates = {JOBS_LIST_KEY: ", ".join(j.name for j in jobs)}
    for j in jobs:
        for key, ui_type, _label, _help in JOB_FIELD_SPECS:
            fid = job_field_id(j.name, key)
            if key == "purge_mode":
                updates[fid] = ("purge_group" if j.purge_group
                                else "purge_unmatched" if j.purge_unmatched
                                else "none")
            elif ui_type == "lines":
                updates[fid] = "\n".join(getattr(j, key))
            elif key in ("start_number", "preserve_below", "preserve_above", "max_past_hours", "max_future_hours"):
                val = getattr(j, key)
                updates[fid] = val if val is not None else 0
            else:
                updates[fid] = getattr(j, key)
        for src in j.epg_sources:
            updates[f"{job_field_id(j.name, EPG_SOURCE_TOGGLE)}:{src}"] = True
    return updates


def jobs_from_settings(settings: dict) -> Tuple[List[SimpleNamespace], List[str]]:
    """
    Build all jobs from the plugin settings.
    Returns (valid_jobs, error_messages) so one misconfigured job doesn't
    block the others from running.
    """
    seeds = load_seed_jobs()
    errors: List[str] = []
    try:
        names = parse_job_names(settings, seeds)
    except JobConfigError as e:
        return [], [str(e)]

    jobs = []
    for name in names:
        try:
            jobs.append(job_from_settings(settings, name, seeds))
        except JobConfigError as e:
            errors.append(
                f"{e} — if you just added this job, press 'Reload job fields', "
                f"refresh the page, and fill in its settings."
            )
    return jobs, errors


# ---------------------------------------------------------------------------
# ORM data access (replaces the REST DispatcharrClient of the CLI script)
# ---------------------------------------------------------------------------

class OrmClient:
    """Thin ORM wrapper exposing the same data shapes the CLI script used."""

    def get_all_streams(self) -> List[Dict]:
        from apps.channels.models import Stream, ChannelStream

        assigned_ids = set(ChannelStream.objects.values_list("stream_id", flat=True))

        # Resolve the User-Agent per m3u account once (used for black-screen
        # probing) to avoid an N+1 query per stream.
        account_ids = set(
            Stream.objects.exclude(m3u_account__isnull=True)
            .values_list("m3u_account_id", flat=True).distinct()
        )
        ua_by_account = self._user_agents_by_account(account_ids)

        streams = []
        qs = Stream.objects.select_related("channel_group", "m3u_account").only(
            "id", "name", "url", "tvg_id", "logo_url",
            "channel_group__name", "m3u_account__name"
        )
        for s in qs.iterator(chunk_size=2000):
            streams.append({
                "id": s.id,
                "name": s.name or "",
                "url": s.url or "",
                "tvg_id": s.tvg_id or "",
                "logo_url": s.logo_url or "",
                "channel_group": s.channel_group.name if s.channel_group_id else "",
                "m3u_account_name": s.m3u_account.name if s.m3u_account_id else "",
                "user_agent": ua_by_account.get(s.m3u_account_id) or DEFAULT_USER_AGENT,
                "channel_id": s.id in assigned_ids,  # truthy = already assigned
            })
        return streams

    @staticmethod
    def _user_agents_by_account(account_ids: set) -> Dict[int, str]:
        """{m3u_account_id: user_agent_string} for the given accounts.

        Resolution failures (missing default UA, deleted rows) are skipped per
        account so callers fall back to DEFAULT_USER_AGENT."""
        ua_map: Dict[int, str] = {}
        if not account_ids:
            return ua_map
        from apps.m3u.models import M3UAccount
        for acc in M3UAccount.objects.filter(id__in=account_ids):
            try:
                ua = acc.get_user_agent().user_agent
            except Exception:
                continue
            if ua:
                ua_map[acc.id] = ua
        return ua_map

    def get_all_channels(self) -> List[Dict]:
        from apps.channels.models import Channel, ChannelStream

        stream_ids_by_channel: Dict[int, List[int]] = defaultdict(list)
        for ch_id, s_id in ChannelStream.objects.order_by("order").values_list(
            "channel_id", "stream_id"
        ):
            stream_ids_by_channel[ch_id].append(s_id)

        channels = []
        qs = Channel.objects.select_related("epg_data").only(
            "id", "name", "channel_number", "channel_group", "tvg_id",
            "epg_data__tvg_id")
        for ch in qs.iterator(chunk_size=2000):
            channels.append({
                "id": ch.id,
                "name": ch.name or "",
                "channel_number": ch.channel_number,
                "channel_group": ch.channel_group_id,
                "tvg_id": ch.tvg_id or "",
                # tvg_id of the EPG assigned in the UI (may differ from the
                # tvg_id text field) — used to inherit streams for EPG matches
                "epg_tvg_id": (ch.epg_data.tvg_id or "") if ch.epg_data_id else "",
                "streams": stream_ids_by_channel.get(ch.id, []),
            })
        return channels

    def get_channel_groups(self) -> List[Dict]:
        from apps.channels.models import ChannelGroup
        return list(ChannelGroup.objects.values("id", "name"))

    def create_channel_group(self, name: str) -> Dict:
        from apps.channels.models import ChannelGroup
        group, _ = ChannelGroup.objects.get_or_create(name=name)
        return {"id": group.id, "name": group.name}

    def delete_channel(self, channel_id: int) -> None:
        from apps.channels.models import Channel
        Channel.objects.filter(id=channel_id).delete()

    def create_channel(self, name: str, stream_ids: List[int],
                       group_id: Optional[int] = None,
                       channel_number: Optional[float] = None,
                       tvg_id: Optional[str] = None,
                       logo_url: Optional[str] = None) -> Dict:
        from django.db import transaction
        from apps.channels.models import Channel, ChannelStream, Logo

        with transaction.atomic():
            logo = None
            if logo_url:
                # Same pattern as Dispatcharr's M3U auto channel sync
                logo, _ = Logo.objects.get_or_create(
                    url=logo_url, defaults={"name": name or "Unknown"})
            ch = Channel.objects.create(
                name=name,
                channel_group_id=group_id,
                channel_number=channel_number,
                tvg_id=tvg_id or None,
                logo=logo,
            )
            ChannelStream.objects.bulk_create([
                ChannelStream(channel=ch, stream_id=sid, order=i)
                for i, sid in enumerate(stream_ids)
            ])
        return {"id": ch.id, "name": ch.name}


# ---------------------------------------------------------------------------
# EPG assignment for created channels
# ---------------------------------------------------------------------------

PLUGIN_EPG_SOURCE_NAME = "Sports Event Auto-Creator"


def ensure_plugin_epg_source():
    """
    The plugin-owned EPG source that holds generated event programmes.
    Inactive on purpose: refresh_all_epg_data only processes active sources,
    so nothing ever fetches or overwrites it. source_type is 'xmltv' (NOT
    'dummy') so grid/outputs use the stored ProgramData rows as-is.
    """
    from apps.epg.models import EPGSource
    src, _ = EPGSource.objects.get_or_create(
        name=PLUGIN_EPG_SOURCE_NAME,
        defaults={"source_type": "xmltv", "is_active": False},
    )
    return src


def resolve_epg_data(tvg_id: str, source_name: Optional[str] = None):
    """
    Find the EPGData row matching an EPG-search hit. Prefer the source the
    programme was found in; fall back to any source with that tvg_id (covers
    external XMLTV URLs whose feed is also ingested in EPG Manager).
    """
    from apps.epg.models import EPGData
    qs = EPGData.objects.filter(tvg_id=tvg_id).select_related("epg_source")
    if source_name and source_name != "xmltv":
        hit = qs.filter(epg_source__name=source_name).first()
        if hit is not None:
            return hit
    return qs.first()


def link_channel_epg(channel_id: int, epg_data) -> None:
    from apps.channels.models import Channel
    Channel.objects.filter(id=channel_id).update(epg_data=epg_data)


def create_event_epg(channel_id: int, title: str, description: str,
                     start_utc_naive: datetime, duration_hours: float):
    """Create (or replace) a single-programme guide entry for an event channel
    under the plugin-owned EPG source, and link the channel to it."""
    from django.db import transaction
    from datetime import timezone as _tz
    from apps.epg.models import EPGData, ProgramData

    src = ensure_plugin_epg_source()
    with transaction.atomic():
        epg_data, created = EPGData.objects.get_or_create(
            tvg_id=f"sea-ch-{channel_id}", epg_source=src,
            defaults={"name": title},
        )
        if not created:
            epg_data.programs.all().delete()
            if epg_data.name != title:
                epg_data.name = title
                epg_data.save(update_fields=["name"])
        start = start_utc_naive.replace(tzinfo=_tz.utc)
        ProgramData.objects.create(
            epg=epg_data,
            start_time=start,
            end_time=start + timedelta(hours=duration_hours),
            title=title,
            description=description or "",
            tvg_id=epg_data.tvg_id,
        )
        link_channel_epg(channel_id, epg_data)
    return epg_data


def cleanup_orphan_epg(logger) -> int:
    """Delete plugin-owned EPGData rows no longer referenced by any channel
    (their ProgramData cascades). Called after each real run."""
    from apps.epg.models import EPGSource, EPGData
    src = EPGSource.objects.filter(name=PLUGIN_EPG_SOURCE_NAME).first()
    if src is None:
        return 0
    orphans = EPGData.objects.filter(epg_source=src, channels__isnull=True)
    count = orphans.count()
    if count:
        orphans.delete()
        logger.info(f"[EPG-ASSIGN] Removed {count} orphaned event guide entr"
                    f"{'y' if count == 1 else 'ies'}")
    return count


def assign_channel_epg(channel_id: int, display_name: str, source: str,
                       reason: str, sort_dt: Optional[datetime],
                       is_uncertain: bool, tvg_id: Optional[str],
                       epg_src_label: Optional[str],
                       duration_hours: float) -> str:
    """
    Give a freshly created channel its EPG. Returns a short outcome string.
    EPG-search hits link the real EPGData row; otherwise (name-search, or an
    external feed not ingested in EPG Manager) a single event programme is
    generated from the parsed title/time when the time is reliable.
    """
    title = display_name.split(" | ", 1)[-1].strip() or display_name

    if source == "EPG" and tvg_id:
        epg_data = resolve_epg_data(tvg_id, epg_src_label)
        if epg_data is not None:
            link_channel_epg(channel_id, epg_data)
            src_name = getattr(epg_data.epg_source, "name", None) or "?"
            return f"linked to EPG source '{src_name}' (tvg_id '{tvg_id}')"

    if sort_dt is not None and not is_uncertain:
        create_event_epg(channel_id, title, str(reason or ""), sort_dt, duration_hours)
        return f"event programme created ({duration_hours:g}h)"

    return "no reliable time — left to Dispatcharr's dummy EPG"


# ---------------------------------------------------------------------------
# XMLTV fetching (Phase 1)
# ---------------------------------------------------------------------------

def load_epg_source_programmes(source_name: str, logger,
                               cache: Optional[Dict] = None) -> List[Tuple]:
    """
    Load programmes of a Dispatcharr EPG source (M3U & EPG Manager) straight
    from the database — Dispatcharr has already fetched and parsed the feed,
    and EPGData carries the original provider tvg_ids that match streams.

    Returns a list of (epg_id, title, desc, start_utc_naive, source_name) tuples.
    """
    from apps.epg.models import EPGSource, ProgramData

    name = (source_name or "").strip()
    src = EPGSource.objects.filter(name__iexact=name).first()
    if src is None:
        available = ", ".join(
            EPGSource.objects.order_by("name").values_list("name", flat=True)
        ) or "(none)"
        raise JobRuntimeError(
            f"EPG source '{name}' not found in M3U & EPG Manager. Available: {available}"
        )

    cache_key = f"epgsource:{src.id}"
    if cache is not None and cache_key in cache:
        logger.info(f"Reusing cached programmes for EPG source '{src.name}'")
        return cache[cache_key]

    from datetime import timezone as _tz
    programmes = []
    qs = (ProgramData.objects.filter(epg__epg_source=src)
          .select_related("epg")
          .only("title", "description", "start_time", "epg__tvg_id"))
    for p in qs.iterator(chunk_size=5000):
        start = p.start_time
        if start is None:
            continue
        if start.tzinfo is not None:
            start = start.astimezone(_tz.utc).replace(tzinfo=None)
        programmes.append((
            p.epg.tvg_id or "",
            (p.title or "").strip(),
            (p.description or "").strip(),
            start,
            src.name,
        ))
    logger.info(f"Loaded {len(programmes)} programmes from EPG source '{src.name}'")
    if cache is not None:
        cache[cache_key] = programmes
    return programmes


def load_epg_sources_programmes(source_names: List[str], logger,
                                cache: Optional[Dict] = None) -> List[Tuple]:
    """Concatenate the programmes of several EPG sources (each cached individually)."""
    programmes: List[Tuple] = []
    for name in source_names:
        programmes.extend(load_epg_source_programmes(name, logger, cache))
    return programmes


def fetch_xmltv(url_or_path: str, logger) -> ET.Element:
    """Fetch and parse XMLTV from a URL or a local file path."""
    if url_or_path.startswith("http"):
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/xml, text/xml, */*; q=0.01",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        logger.info(f"Fetching XMLTV from {url_or_path}...")
        try:
            response = requests.get(url_or_path, headers=headers, timeout=60,
                                    allow_redirects=True)
            response.raise_for_status()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            raise JobRuntimeError(f"HTTP error {status} while fetching XMLTV")
        except Exception as e:
            raise JobRuntimeError(f"Failed to download XMLTV: {e}")

        content = response.content
        if not content:
            raise JobRuntimeError("Downloaded XMLTV content is empty")

        if content.startswith(b"\x1f\x8b"):
            logger.info("Decompressing gzipped XMLTV...")
            try:
                content = gzip.decompress(content)
            except Exception as e:
                raise JobRuntimeError(f"Failed to decompress gzipped XMLTV: {e}")

        try:
            return ET.fromstring(content)
        except Exception as e:
            snippet = content[:200].decode("utf-8", errors="ignore").strip()
            if snippet.lower().startswith(("<!doctype html", "<html")):
                raise JobRuntimeError(
                    "Received HTML instead of XMLTV data (URL truncated or expired?)"
                )
            raise JobRuntimeError(f"Failed to parse XMLTV: {e} (starts with: {snippet[:80]})")
    else:
        logger.info(f"Reading XMLTV from {url_or_path}...")
        try:
            return ET.parse(url_or_path).getroot()
        except Exception as e:
            raise JobRuntimeError(f"Failed to read local XMLTV file: {e}")


# ---------------------------------------------------------------------------
# Job runner (port of the CLI script's main())
# ---------------------------------------------------------------------------

def _has_reliable_date(raw_name: str) -> bool:
    """
    require_time gate: the stream name must carry an actual date — an explicit
    day+month/ISO date (is_specific) or at least a weekday (pins the event to
    the next occurrence). A bare time like '8:10pm' is NOT enough: the parser
    assumes "today" for it, so a days-old event name would pass the past/future
    filters again every day and keep recreating a dead channel.
    """
    dt, is_specific, weekday = engine.extract_datetime_from_stream_name(raw_name)
    return dt is not None and (is_specific or weekday is not None)


def _is_preserved_number(ch_num, job) -> bool:
    """Full-purge protection: channel numbers strictly below `preserve_below`
    and/or strictly above `preserve_above` are never purged (curated channels
    at the start of the group, 24/7 channels appended at the end)."""
    if ch_num is None:
        return False
    if job.preserve_below is not None and ch_num < job.preserve_below:
        return True
    if job.preserve_above is not None and ch_num > job.preserve_above:
        return True
    return False


def _has_excluded_prefix(stream_name: str, prefixes: List[str]) -> bool:
    """EPG-phase country/provider filter: does the stream name start with one
    of the excluded prefixes (e.g. 'SKY:', 'PL:')? Case-insensitive; both the
    name and the prefixes are stripped so 'SKY: ' and 'SKY:' behave the same."""
    name = (stream_name or "").lstrip().lower()
    return any(name.startswith(p) for p in
               (p.strip().lower() for p in prefixes) if p)


# ---------------------------------------------------------------------------
# Black-screen probing (EPG phase only)
# ---------------------------------------------------------------------------

def probe_stream_black(url: str, user_agent: str, sample_seconds: float,
                       logger) -> Tuple[str, Optional[float], str]:
    """
    Sample a few seconds of a stream with ffmpeg's signalstats filter and
    classify it as black / good / indeterminate (see engine.classify_probe).

    Returns ``(verdict, mean_yavg, reason)``; `reason` explains an
    INDETERMINATE verdict (ffmpeg error line, timeout, ...) for the log.
    Any failure (missing url, ffmpeg absent, subprocess timeout, nonzero exit
    with no samples) yields INDETERMINATE so the caller keeps the stream —
    only a provably black stream is rejected.
    """
    if not url:
        return engine.PROBE_INDETERMINATE, None, "stream has no URL"
    if shutil.which("ffmpeg") is None:
        return engine.PROBE_INDETERMINATE, None, "ffmpeg not found"

    # -timeout is in microseconds and applies to network protocols; -t is an
    # output option (after -i) capping how much of the stream we decode.
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-v", "info",
        "-user_agent", user_agent or DEFAULT_USER_AGENT,
        "-timeout", "15000000",
        "-i", url,
        "-t", str(sample_seconds),
        "-vf", "signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG",
        "-an", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True,
                              timeout=sample_seconds + 30)
        stderr = proc.stderr.decode("utf-8", errors="ignore")
    except subprocess.TimeoutExpired:
        return (engine.PROBE_INDETERMINATE, None,
                f"probe timed out after {sample_seconds + 30:.0f}s")
    except Exception as e:
        return engine.PROBE_INDETERMINATE, None, f"probe failed: {e}"

    samples = engine.parse_yavg_samples(stderr)
    verdict, mean = engine.classify_probe(samples)
    reason = ""
    if verdict == engine.PROBE_INDETERMINATE:
        err_line = engine.pick_error_line(stderr)
        if proc.returncode != 0:
            reason = f"ffmpeg exit {proc.returncode}"
            if err_line:
                reason += f": {err_line}"
        else:
            reason = "no video frames decoded"
    return verdict, mean, reason


def _select_unblack_streams(streams: List[Dict], needed: Optional[int],
                            probe_state: Dict, job_name: str, title: str,
                            logger) -> List[Dict]:
    """
    Probe candidate streams (in their existing priority order) and return only
    the confirmed-good ones: black screens AND failed probes (timeout, HTTP
    error, no decodable video) are both rejected — a probed stream must prove
    it plays. Streams the run *couldn't* probe at all (ffmpeg missing, probe
    budget exhausted) are kept, appended after the good ones: that's a system
    limitation, not evidence against the stream.

    Results are cached in probe_state["cache"] (shared across events and jobs
    in one run) and constrained by probe_state["budget"]. When `needed` is not
    None (split mode with max_split>0), probing stops early once that many good
    streams are found. An empty return means every candidate failed the check.
    """
    cache = probe_state["cache"]
    sample_seconds = probe_state["sample_seconds"]

    ffmpeg_ok = shutil.which("ffmpeg") is not None
    if not ffmpeg_ok and not probe_state.get("ffmpeg_missing_logged"):
        logger.warning(f"[{job_name}] [BLACK-CHECK] ffmpeg not found — cannot probe "
                       f"streams; keeping all candidates this run")
        probe_state["ffmpeg_missing_logged"] = True

    good: List[Dict] = []
    kept_unprobed: List[Dict] = []
    for s in streams:
        sid = s["id"]
        name = s.get("name", "")

        if sid in cache:
            verdict, mean, reason = cache[sid]
        elif not ffmpeg_ok or probe_state["budget"] <= 0:
            # Couldn't probe (system-level): keep, but never ahead of a
            # confirmed-good stream.
            if ffmpeg_ok and not probe_state.get("budget_exhausted_logged"):
                logger.warning(f"[{job_name}] [BLACK-CHECK] Probe budget exhausted for "
                               f"this run — remaining streams kept unprobed")
                probe_state["budget_exhausted_logged"] = True
            kept_unprobed.append(s)
            continue
        else:
            verdict, mean, reason = probe_stream_black(
                s.get("url", ""), s.get("user_agent", ""), sample_seconds, logger)
            probe_state["budget"] -= 1
            cache[sid] = (verdict, mean, reason)
            if verdict == engine.PROBE_GOOD:
                logger.info(f"[{job_name}] [BLACK-CHECK] '{title}': stream '{name}' "
                            f"→ good (YAVG {mean:.1f})")
            elif verdict == engine.PROBE_BLACK:
                logger.info(f"[{job_name}] [BLACK-CHECK] '{title}': stream '{name}' "
                            f"→ BLACK (YAVG {mean:.1f}) — skipped")
            else:
                detail = f" — {reason}" if reason else ""
                logger.info(f"[{job_name}] [BLACK-CHECK] '{title}': stream '{name}' "
                            f"→ probe failed, skipped{detail}")

        if verdict == engine.PROBE_GOOD:
            good.append(s)
        # PROBE_BLACK and PROBE_INDETERMINATE (probe attempted): rejected.

        if needed is not None and len(good) >= needed:
            break

    return good + kept_unprobed


def run_job(job: SimpleNamespace, logger, dry_run: bool = False,
            xmltv_cache: Optional[Dict[str, ET.Element]] = None,
            assign_epg: bool = True,
            event_duration_hours: float = 3.0,
            use_stream_logo: bool = True,
            probe_state: Optional[Dict] = None) -> Dict:
    """
    Execute one job. Returns a stats dict:
    {prepared, created, deleted, skipped, preserved, errors}.

    xmltv_cache lets multiple jobs sharing one XMLTV URL/EPG source fetch it
    only once per run. With assign_epg, created channels are linked to real
    EPG data (EPG-search hits) or get a generated event programme
    (name-search hits with a reliable time).
    """
    client = OrmClient()
    stats = {"prepared": 0, "created": 0, "deleted": 0,
             "skipped": 0, "preserved": 0, "errors": 0}

    channels_to_create: List[Tuple] = []
    all_matched_stream_ids: set = set()

    logger.info(f"[{job.name}] Fetching all streams and channels from the database...")
    all_streams = client.get_all_streams()
    all_existing_channels = client.get_all_channels()

    # Build stream map (EPG ID -> list of stream dicts)
    stream_map = defaultdict(list)
    _stream_ids_per_epg = defaultdict(set)
    streams_by_id = {s["id"]: s for s in all_streams}

    # 1. Map streams by their channel's EPG IDs (inheritance): both the
    #    tvg_id text field and the EPG assigned in the UI (epg_data)
    for ch in all_existing_channels:
        eids = {(ch.get("tvg_id") or "").strip(),
                (ch.get("epg_tvg_id") or "").strip()}
        eids.discard("")
        for eid in eids:
            for sid in ch.get("streams", []):
                if sid in streams_by_id and sid not in _stream_ids_per_epg[eid]:
                    stream_map[eid].append(streams_by_id[sid])
                    _stream_ids_per_epg[eid].add(sid)

    # 2. Map streams by their own EPG ID (self-identification)
    for s in all_streams:
        eid = (s.get("tvg_id") or "").strip()
        if eid and s["id"] not in _stream_ids_per_epg[eid]:
            stream_map[eid].append(s)
            _stream_ids_per_epg[eid].add(s["id"])

    # Reference time
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    now_local = engine.convert_utc_to_display(now_utc)

    # Filtering range
    target_date = None
    if job.date:
        target_date = datetime.strptime(job.date, "%Y-%m-%d").date()
        logger.info(f"[{job.name}] Target date set to: {target_date}")
    elif job.today_only:
        target_date = now_local.date()

    max_upcoming_date = None
    if job.upcoming:
        max_upcoming_date = now_local.date() + timedelta(days=job.days - 1)

    # ----------------------------- PHASE 1: EPG -----------------------------
    if job.epg_sources or job.xmltv_url:
        if job.epg_sources:
            if job.xmltv_url:
                logger.info(f"[{job.name}] Both EPG source(s) and XMLTV URL are set — "
                            f"using EPG source(s): {', '.join(job.epg_sources)}")
            # (epg_id, title, desc, start_utc) tuples from the database
            programmes = load_epg_sources_programmes(job.epg_sources, logger, xmltv_cache)
        else:
            if xmltv_cache is not None and job.xmltv_url in xmltv_cache:
                root = xmltv_cache[job.xmltv_url]
                logger.info(f"[{job.name}] Reusing cached XMLTV for {job.xmltv_url}")
            else:
                root = fetch_xmltv(job.xmltv_url, logger)
                if xmltv_cache is not None:
                    xmltv_cache[job.xmltv_url] = root

            def _iter_xmltv(root=root):
                for programme in root.findall("programme"):
                    title_el = programme.find("title")
                    desc_el = programme.find("desc")
                    yield (programme.get("channel") or "",
                           (title_el.text or "").strip() if title_el is not None else "",
                           (desc_el.text or "").strip() if desc_el is not None else "",
                           programme.get("start", ""),  # parsed lazily after matching
                           "xmltv")

            programmes = _iter_xmltv()

        logger.info(f"[{job.name}] PHASE 1: EPG-based search")

        programmes_by_event = defaultdict(set)  # (title, start_utc) -> {(epg_id, source)}
        epg_filtered_count = 0

        for epg_id, title, desc, start_val, src_label in programmes:
            matches = False
            if not job.search:
                matches = True
            else:
                for term in job.search:
                    term = term.strip()
                    if not term:
                        continue
                    pattern = rf"\b{re.escape(term)}\b"
                    if re.search(pattern, title, re.IGNORECASE) or (
                            job.search_descriptions
                            and re.search(pattern, desc, re.IGNORECASE)):
                        matches = True
                        break
            if matches and job.exclude:
                for ex in job.exclude:
                    if engine.exclude_matches(ex, title, desc):
                        matches = False
                        break

            if matches:
                start_utc = (start_val if isinstance(start_val, datetime)
                             else engine.parse_xmltv_time(start_val or ""))
                if start_utc:
                    prog_local = engine.convert_utc_to_display(start_utc)

                    if target_date and prog_local.date() != target_date:
                        epg_filtered_count += 1
                        continue
                    if job.upcoming and (prog_local.date() < now_local.date()
                                         or prog_local.date() > max_upcoming_date):
                        epg_filtered_count += 1
                        continue
                    if job.future_only and start_utc < now_utc:
                        epg_filtered_count += 1
                        continue
                    if job.max_past_hours is not None and \
                            start_utc < now_utc - timedelta(hours=job.max_past_hours):
                        epg_filtered_count += 1
                        continue
                    if job.max_future_hours is not None and \
                            start_utc > now_utc + timedelta(hours=job.max_future_hours):
                        epg_filtered_count += 1
                        continue

                    programmes_by_event[(title, start_utc)].add((epg_id, src_label))

        if epg_filtered_count > 0:
            logger.info(f"[{job.name}] [EPG] Filtered out {epg_filtered_count} programmes by date/time")

        sorted_items = sorted(programmes_by_event.items(), key=lambda x: x[0][1])
        logger.info(f"[{job.name}] [EPG] Found {len(sorted_items)} unique matching events")

        for (title, start_utc), id_pairs in sorted_items:
            epg_ids = {p[0] for p in id_pairs}
            local_time = engine.convert_utc_to_display(start_utc)
            logger.info(f"[{job.name}] [EPG] '{title}' @ {local_time.strftime('%H:%M %d-%b')} "
                        f"— found in: {', '.join(sorted({p[1] for p in id_pairs}))}")

            all_event_streams = []
            seen_sid = set()
            for eid in epg_ids:
                for s in stream_map.get(eid, []):
                    if s["id"] not in seen_sid:
                        all_event_streams.append(s)
                        seen_sid.add(s["id"])

            if not all_event_streams:
                logger.info(f"[{job.name}] [EPG] Skipped '{title}': no streams are "
                            f"linked to its EPG id(s): {', '.join(sorted(epg_ids))}")
                continue

            streams = all_event_streams
            dropped_assigned = dropped_excluded = dropped_prefix = 0
            if job.unassigned:
                kept = [s for s in streams if not s.get("channel_id")]
                dropped_assigned = len(streams) - len(kept)
                streams = kept
            if job.exclude:
                kept = [s for s in streams
                        if not any(engine.exclude_matches(ex, s.get("name", ""))
                                   for ex in job.exclude)]
                dropped_excluded = len(streams) - len(kept)
                streams = kept
            if job.exclude_stream_prefixes:
                kept = [s for s in streams
                        if not _has_excluded_prefix(s.get("name", ""),
                                                    job.exclude_stream_prefixes)]
                dropped_prefix = len(streams) - len(kept)
                streams = kept
            reasons = []
            if dropped_assigned:
                reasons.append(f"{dropped_assigned} already assigned to a channel")
            if dropped_excluded:
                reasons.append(f"{dropped_excluded} matched an exclude term")
            if dropped_prefix:
                reasons.append(f"{dropped_prefix} matched an excluded name prefix")
            if not streams:
                logger.info(f"[{job.name}] [EPG] Skipped '{title}': all "
                            f"{len(all_event_streams)} candidate streams dropped "
                            f"({'; '.join(reasons) or 'filtered'})")
                continue
            if reasons:
                # Partial drops are easy to miss: with split_streams they can
                # silently yield fewer channels than max_split allows.
                logger.info(f"[{job.name}] [EPG] '{title}': kept {len(streams)} of "
                            f"{len(all_event_streams)} candidate streams "
                            f"({'; '.join(reasons)})")

            # Black-screen filtering: skip candidate streams that are a
            # permanent black screen and fail over to the next one. EPG-match
            # streams are regular 24/7 channels (matched via tvg_id), so they
            # should never be black — probe them regardless of whether the
            # event has started. (Name-search event slots, which ARE black
            # before their event starts, are never probed — see Phase 2.)
            # Runs in dry runs too (read-only) so verdicts can be previewed.
            if job.check_black and probe_state is not None:
                needed = ((job.max_split if job.max_split > 0 else None)
                          if job.split_streams else None)
                selected = _select_unblack_streams(
                    streams, needed, probe_state, job.name, title, logger)
                if not selected:
                    logger.info(f"[{job.name}] [EPG] Skipped '{title}': all "
                                f"{len(streams)} candidate streams are black "
                                f"screens or failed probing")
                    continue
                streams = selected

            epg_country_flag = ""
            if job.country_flags:
                for s in streams:
                    epg_country_flag = engine.detect_flag_from_stream(
                        s.get("name", ""), s.get("channel_group", ""),
                        s.get("m3u_account_name", ""))
                    if epg_country_flag:
                        break

            base_display_name = engine.format_epg_channel_name(title, start_utc, epg_country_flag)

            # Deterministic (tvg_id, source) reference for EPG assignment
            ref_eid, ref_src = sorted(id_pairs)[0]

            if job.split_streams:
                streams_to_use = streams[:job.max_split] if job.max_split > 0 else streams
                for s in streams_to_use:
                    all_matched_stream_ids.add(s["id"])
                    channels_to_create.append(
                        (base_display_name, [s["id"]], "EPG", title, start_utc, False,
                         ref_eid, ref_src))
            else:
                ids = [s["id"] for s in streams]
                all_matched_stream_ids.update(ids)
                channels_to_create.append(
                    (base_display_name, ids, "EPG", title, start_utc, False,
                     ref_eid, ref_src))

        logger.info(f"[{job.name}] [EPG] Prepared {sum(1 for c in channels_to_create if c[2] == 'EPG')} channels")

    # ----------------------------- PHASE 2: NAME ----------------------------
    # Note: the `upcoming` window is deliberately applied only in the EPG phase
    # above. Name-based streams are treated as "current" (their embedded times
    # are often relative/unreliable), so _passes_time_filters intentionally
    # checks only target_date/future_only/max_past_hours/max_future_hours.
    if job.search:
        logger.info(f"[{job.name}] PHASE 2: name-based search (with timezone inference)")
        found_streams_by_id: Dict[int, Dict] = {}
        for term in job.search:
            t = term.strip().lower()
            if not t:
                continue
            for s in all_streams:
                if t in s["name"].lower():
                    found_streams_by_id[s["id"]] = s
        logger.info(f"[{job.name}] [NAME] Found {len(found_streams_by_id)} streams")

        by_event_name = defaultdict(list)
        for s_id, s in found_streams_by_id.items():
            if s_id in all_matched_stream_ids:
                continue

            raw_name = s.get("name", "")
            cleaned_title = engine.clean_stream_name(raw_name)

            if job.exclude and any(engine.exclude_matches(ex, raw_name, cleaned_title)
                                   for ex in job.exclude):
                continue
            if job.unassigned and s.get("channel_id"):
                continue

            by_event_name[cleaned_title.lower()].append(s)

        confirmed_count, uncertain_count, name_filtered_count = 0, 0, 0

        def _passes_time_filters(sort_dt):
            """Apply date/time filters. Returns True if the entry survives.

            engine.build_name_channel_name always returns sort_dt as naive UTC
            (best-guess UTC for uncertain-timezone streams), so all comparisons
            here are done in UTC; only the target_date check converts to the
            display timezone first.
            """
            nonlocal name_filtered_count
            check_dt_local = engine.convert_utc_to_display(sort_dt)

            if target_date and check_dt_local.date() != target_date:
                name_filtered_count += 1
                return False
            if job.future_only:
                if sort_dt < now_utc:
                    name_filtered_count += 1
                    return False
            if job.max_past_hours is not None:
                if sort_dt < now_utc - timedelta(hours=job.max_past_hours):
                    name_filtered_count += 1
                    return False
            if job.max_future_hours is not None:
                if sort_dt > now_utc + timedelta(hours=job.max_future_hours):
                    name_filtered_count += 1
                    return False
            return True

        for _, streams_list in by_event_name.items():
            if job.split_streams:
                streams_to_use = streams_list[:job.max_split] if job.max_split > 0 else streams_list
                for s in streams_to_use:
                    raw_name = s.get("name", "")
                    if job.require_time and not _has_reliable_date(raw_name):
                        name_filtered_count += 1
                        continue
                    display_name, sort_dt, tz_confidence, region_label = engine.build_name_channel_name(
                        raw_name, s, job.no_region_label, job.country_flags)

                    if not _passes_time_filters(sort_dt):
                        continue

                    is_uncertain = (tz_confidence == engine.TZ_UNCERTAIN)
                    if is_uncertain:
                        uncertain_count += 1
                    else:
                        confirmed_count += 1

                    all_matched_stream_ids.add(s["id"])
                    tvg_id_for_channel = (s.get("tvg_id") or "").strip() or None
                    channels_to_create.append(
                        (display_name, [s["id"]], "NAME", raw_name, sort_dt, is_uncertain,
                         tvg_id_for_channel, None))
            else:
                all_ids = [s["id"] for s in streams_list]
                representative = streams_list[0]
                raw_name = representative.get("name", "")
                if job.require_time and not _has_reliable_date(raw_name):
                    name_filtered_count += 1
                    continue
                display_name, sort_dt, tz_confidence, region_label = engine.build_name_channel_name(
                    raw_name, representative, job.no_region_label, job.country_flags)

                if not _passes_time_filters(sort_dt):
                    continue

                is_uncertain = (tz_confidence == engine.TZ_UNCERTAIN)
                if is_uncertain:
                    uncertain_count += 1
                else:
                    confirmed_count += 1

                all_matched_stream_ids.update(all_ids)
                tvg_id_for_channel = (representative.get("tvg_id") or "").strip() or None
                channels_to_create.append(
                    (display_name, all_ids, "NAME", raw_name, sort_dt, is_uncertain,
                     tvg_id_for_channel, None))

        if name_filtered_count > 0:
            logger.info(f"[{job.name}] [NAME] Filtered out {name_filtered_count} streams by date/time flags")
        logger.info(f"[{job.name}] [NAME] Timezone confirmed: {confirmed_count}, uncertain: {uncertain_count}")

    # --------------------------- SORT & PIN-TOP -----------------------------
    stats["prepared"] = len(channels_to_create)
    if channels_to_create:
        # Reliably-timed channels first in chronological order, then uncertain.
        channels_to_create.sort(key=lambda x: (bool(x[5]), x[4] if x[4] else datetime.now()))
        if job.pin_top:
            pin_terms = [t.lower() for t in job.pin_top if t and t.strip()]
            num_pins = len(pin_terms)

            def _pin_rank(ch):
                haystack = f"{ch[0]} {ch[3]}".lower()
                for i, term in enumerate(pin_terms):
                    if term in haystack:
                        return i
                return num_pins

            channels_to_create.sort(key=_pin_rank)  # stable: keeps chrono order

    # ------------------------------ GROUP ------------------------------------
    target_group_id = None
    actually_deleted_stream_ids, actually_deleted_names = set(), set()
    groups = client.get_channel_groups()
    existing_group = next((g for g in groups if g["name"].lower() == job.group.lower()), None)
    if existing_group:
        target_group_id = existing_group["id"]
        logger.info(f"[{job.name}] Found group: '{job.group}' ({target_group_id})")
    elif not dry_run:
        new_group = client.create_channel_group(job.group)
        target_group_id = new_group["id"]
        logger.info(f"[{job.name}] Created group: '{job.group}' ({target_group_id})")

    # ------------------------------ CLEANUP ----------------------------------
    if target_group_id:
        group_channels = [ch for ch in all_existing_channels
                          if ch.get("channel_group") == target_group_id]
        prepared_names = {c[0] for c in channels_to_create}

        for ch in group_channels:
            ch_name = ch.get("name", "")
            should_delete = False
            reason = ""

            # 0. Purge entire group — takes precedence over all other rules
            if job.purge_group:
                if _is_preserved_number(ch.get("channel_number"), job):
                    should_delete = False
                else:
                    should_delete = True
                    reason = "purge group (full recreate)"

            # 1. Cleanup by exclusion
            elif job.cleanup and job.exclude:
                for ex in job.exclude:
                    if engine.exclude_matches(ex, ch_name):
                        should_delete = True
                        reason = f"matches exclude '{ex}'"
                        break

            # 2. Cleanup by age
            if not should_delete and job.cleanup and job.max_past_hours is not None:
                if engine.is_channel_too_old(ch_name, job.max_past_hours):
                    should_delete = True
                    reason = f"older than {job.max_past_hours}h"

            # 3. Purge unmatched
            if not should_delete and job.purge_unmatched:
                if ch_name not in prepared_names:
                    if not any(sid in all_matched_stream_ids for sid in ch.get("streams", [])):
                        should_delete = True
                        reason = "not in current results and streams unmatched"
                    else:
                        should_delete = True
                        reason = "outdated event (streams reused by a newer event)"

            if should_delete:
                if dry_run:
                    logger.info(f"[{job.name}] [DRY RUN] Would delete ({reason}): '{ch_name}'")
                    actually_deleted_stream_ids.update(ch.get("streams", []))
                    actually_deleted_names.add(ch_name)
                    stats["deleted"] += 1
                else:
                    try:
                        client.delete_channel(ch["id"])
                        logger.info(f"[{job.name}] Deleted ({reason}): '{ch_name}'")
                        actually_deleted_stream_ids.update(ch.get("streams", []))
                        actually_deleted_names.add(ch_name)
                        stats["deleted"] += 1
                    except Exception as e:
                        logger.error(f"[{job.name}] Failed to delete '{ch_name}': {e}")
                        stats["errors"] += 1

    # ------------------------------ CREATE -----------------------------------
    # When the target group doesn't exist yet (dry run against a not-yet-created
    # group), target_group_id is None. Matching channel_group == None would hit
    # ungrouped channels and produce misleading "already exists" previews, so
    # treat the group as empty in that case.
    existing_names = set()
    streams_in_group = set()
    if target_group_id is not None:
        existing_names = {ch.get("name") for ch in all_existing_channels
                          if ch.get("channel_group") == target_group_id}
        existing_names -= actually_deleted_names
        streams_in_group = {sid for ch in all_existing_channels
                            if ch.get("channel_group") == target_group_id
                            for sid in ch.get("streams", [])}
        streams_in_group -= actually_deleted_stream_ids

    preserved_names = set()
    preserved_stream_ids = set()
    if target_group_id is not None and job.purge_group and (
            job.preserve_below is not None or job.preserve_above is not None):
        for ch in all_existing_channels:
            if ch.get("channel_group") == target_group_id:
                if _is_preserved_number(ch.get("channel_number"), job):
                    preserved_names.add(ch.get("name", ""))
                    preserved_stream_ids.update(ch.get("streams", []))

    logger.info(f"[{job.name}] Creating/updating {len(channels_to_create)} channels...")
    current_chan_num = job.start_number
    for display_name, ids, source, reason, sort_dt, is_uncertain, tvg_id, epg_src_label in channels_to_create:
        if display_name in existing_names or any(sid in streams_in_group for sid in ids):
            if display_name in preserved_names or any(sid in preserved_stream_ids for sid in ids):
                logger.info(f"[{job.name}] [PRESERVED] [{source}] Kept (below threshold): '{display_name}'")
                stats["preserved"] += 1
                if current_chan_num is not None:
                    current_chan_num += 1
                continue
            logger.info(f"[{job.name}] [SKIPPED] [{source}] Already exists in group: '{display_name}'")
            stats["skipped"] += 1
            if current_chan_num is not None:
                current_chan_num += 1
            continue

        num_info = f" (#{current_chan_num})" if current_chan_num is not None else ""
        if dry_run:
            logger.info(f"[{job.name}] [DRY RUN] [{source}] Would create: '{display_name}' | Streams: {ids}{num_info}")
            stats["created"] += 1
        else:
            try:
                logo_url = None
                if use_stream_logo:
                    logo_url = next(
                        (lu for sid in ids
                         if (lu := (streams_by_id.get(sid) or {}).get("logo_url"))),
                        None)
                new_ch = client.create_channel(display_name, ids, target_group_id,
                                               current_chan_num, tvg_id, logo_url=logo_url)
                logger.info(f"[{job.name}] [{source}] Created: '{display_name}'{num_info}")
                stats["created"] += 1
                if assign_epg:
                    try:
                        outcome = assign_channel_epg(
                            new_ch["id"], display_name, source, reason, sort_dt,
                            is_uncertain, tvg_id, epg_src_label, event_duration_hours)
                        logger.info(f"[{job.name}] [EPG-ASSIGN] {outcome}: '{display_name}'")
                    except Exception as e:
                        # EPG is a nicety; never fail the run over it.
                        logger.error(f"[{job.name}] [EPG-ASSIGN] Failed for '{display_name}': {e}")
            except Exception as e:
                logger.error(f"[{job.name}] [{source}] Failed to create '{display_name}': {e}")
                stats["errors"] += 1
        if current_chan_num is not None:
            current_chan_num += 1

    if assign_epg and not dry_run:
        try:
            cleanup_orphan_epg(logger)
        except Exception:
            logger.exception(f"[{job.name}] [EPG-ASSIGN] Orphan cleanup failed")

    return stats
