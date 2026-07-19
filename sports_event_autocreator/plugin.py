"""
Sports Event Auto-Creator — Dispatcharr plugin.

Automatically creates (and cleans up) event channels for sports, driven by
per-sport "jobs" (search terms, target group, numbering, filters) that are
edited directly in the plugin settings UI, on a user-selected schedule.

The per-job fields are generated dynamically from the "Job names" setting:
the loader re-reads `fields` from this class whenever plugin discovery
reloads, and the "Reload job fields" action bumps the loader's reload token
to force that.

Converted from the standalone CLI script
3-dispatcharr_hybrid_checker_fallback_ZAI_CLAUDE_v6.py.
"""

import os

# Importing tasks at module level registers the @shared_task with Celery in
# every process that loads this plugin (web workers at app-ready, Celery
# workers via the worker_ready hook). Do not remove.
from . import tasks
from . import runner


GLOBAL_FIELDS = [
    {
        "id": "schedule_info",
        "label": "Scheduling",
        "type": "info",
        "description": (
            "Set how often the auto-creation runs. Use the interval in minutes, or a "
            "5-part cron expression for full control (cron wins when both are set). "
            "Press 'Apply schedule' after changing these. Interval 0 with no cron "
            "disables scheduled runs."
        ),
    },
    {
        "id": "interval_minutes",
        "label": "Run every N minutes",
        "type": "number",
        "default": 30,
        "help_text": "How often to run all enabled jobs automatically (0 = disabled).",
    },
    {
        "id": "cron_expression",
        "label": "Cron expression (optional, overrides interval)",
        "type": "string",
        "default": "",
        "placeholder": "e.g. */20 8-23 * * *",
        "help_text": "5-part cron (minute hour day month weekday) in the system timezone.",
    },
    {
        "id": "display_timezone",
        "label": "Display timezone",
        "type": "string",
        "default": "Europe/Madrid",
        "help_text": "IANA timezone used for the times shown in channel names.",
    },
    {
        "id": "assign_epg",
        "label": "Assign EPG to created channels",
        "type": "boolean",
        "default": True,
        "help_text": (
            "EPG-search channels are linked to the matched EPG source's guide data; "
            "name-search channels with a reliable time get a generated event programme "
            "(stored under the inactive 'Sports Event Auto-Creator' EPG source)."
        ),
    },
    {
        "id": "event_duration_hours",
        "label": "Generated event programme duration (hours)",
        "type": "number",
        "default": 3,
        "help_text": "Length of the guide entry generated for name-search event channels.",
    },
    {
        "id": "use_stream_logo",
        "label": "Use stream logo for created channels",
        "type": "boolean",
        "default": True,
        "help_text": "Created channels take the logo of their first stream that has one.",
    },
    {
        "id": "black_sample_seconds",
        "label": "Black-screen probe seconds per stream",
        "type": "number",
        "default": 5,
        "help_text": (
            "Seconds of each stream ffmpeg samples when checking for a black "
            "screen (clamped to 3–30). Only used by jobs with 'Skip black-screen "
            "streams' enabled."
        ),
    },
    {
        "id": "dvr_info",
        "label": "Auto-DVR / Replays",
        "type": "info",
        "description": (
            "Selected event channels can be auto-recorded by Dispatcharr's DVR. "
            "Recording is OPT-IN and per job: set 'Auto-record: title patterns' on "
            "a job (below) to record only the events you care about — an empty "
            "pattern list records nothing. These global settings control the "
            "padding, retention, and an optional watcher for Teamarr event channels."
        ),
    },
    {
        "id": "record_pre_pad_min",
        "label": "Recording pre-roll padding (minutes)",
        "type": "number",
        "default": 5,
        "help_text": "Start each auto-recording this many minutes before the event start time.",
    },
    {
        "id": "record_post_pad_min",
        "label": "Recording post-roll padding (minutes)",
        "type": "number",
        "default": 30,
        "help_text": "Keep recording this many minutes after the event's scheduled end (overruns).",
    },
    {
        "id": "replay_retention_days",
        "label": "Replay retention (days)",
        "type": "number",
        "default": 14,
        "help_text": (
            "Delete auto-created recordings (files + rows) once they are older than "
            "this many days. 0 disables age-based deletion. Failed/zero-byte "
            "auto-recordings are always cleaned up after 1 day. Manual recordings "
            "are never touched."
        ),
    },
    {
        "id": "max_simultaneous_recordings",
        "label": "Max simultaneous recordings (0 = unlimited)",
        "type": "number",
        "default": 2,
        "help_text": (
            "Caps how many recordings (any origin, manual or auto) may be airing "
            "at once — genuinely distinct events can overlap and your provider's "
            "concurrent-stream budget is finite. Duplicate feeds of one broadcast "
            "are already deduplicated by event identity, so this only gates "
            "different events. Recordings whose status marks them dead "
            "(interrupted/failed/stopped/completed) do not occupy a slot. Extra "
            "matches beyond the cap are skipped and logged, not queued."
        ),
    },
    {
        "id": "record_teamarr_groups",
        "label": "Teamarr watcher: channel groups (one per line)",
        "type": "text",
        "default": "",
        "help_text": (
            "Optional. Names of channel groups holding Teamarr event channels "
            "(tvg-id prefix 'teamarr-event-'). Their EPG programmes are watched "
            "and matching ones auto-recorded. Empty = watcher off."
        ),
    },
    {
        "id": "record_teamarr_patterns",
        "label": "Teamarr watcher: title patterns (one per line)",
        "type": "text",
        "default": "",
        "help_text": (
            "Record a Teamarr programme only when its title matches at least one "
            "of these terms (whole-word, same syntax as the job Search terms). "
            "Empty = record nothing."
        ),
    },
    {
        "id": "record_teamarr_exclude",
        "label": "Teamarr watcher: exclude patterns (one per line)",
        "type": "text",
        "default": "",
        "help_text": "Teamarr programme titles matching any of these are never recorded.",
    },
    {
        "id": "job_filter",
        "label": "Job name for 'Run one job'",
        "type": "string",
        "default": "",
        "placeholder": "e.g. boxeo",
        "help_text": "Only used by the 'Run one job' action.",
    },
    {
        "id": "jobs_info",
        "label": "Jobs",
        "type": "info",
        "description": (
            "Each job is one auto-creation search (like one of the old shell scripts) "
            "and has its own group of settings below. To add or remove a job: edit the "
            "'Job names' list, press Save, press 'Reload job fields', then refresh this "
            "page — the job's settings fields will appear/disappear. Removed jobs keep "
            "their saved values, so re-adding the same name restores them."
        ),
    },
    {
        "id": runner.JOBS_LIST_KEY,
        "label": "Job names (comma-separated)",
        "type": "string",
        "help_text": "e.g. Track & Field, Boxing, Futsal, Tennis",
    },
    {
        "id": "transfer_json",
        "label": "Jobs JSON (import / export box)",
        "type": "text",
        "default": "",
        "help_text": (
            "To IMPORT: paste a jobs JSON array here, press Save, then press "
            "'Import jobs JSON' (replaces all job settings). To EXPORT: press "
            "'Export jobs JSON', then refresh this page — the JSON appears in "
            "this box, ready to copy."
        ),
    },
]


def _saved_settings() -> dict:
    """Current persisted settings; tolerant of a missing/unready database."""
    try:
        from apps.plugins.models import PluginConfig
        cfg = PluginConfig.objects.filter(key=tasks.PLUGIN_KEY).first()
        return dict(cfg.settings or {}) if cfg else {}
    except Exception:
        return {}


def _active_epg_source_names() -> list:
    """Names of the active EPG sources in M3U & EPG Manager (unready-DB safe)."""
    try:
        from apps.epg.models import EPGSource
        return list(EPGSource.objects.filter(is_active=True)
                    .order_by("name").values_list("name", flat=True))
    except Exception:
        return []


def _epg_source_toggle_fields(job_name: str, settings: dict, active_sources: list) -> list:
    """
    One checkbox per EPG source for this job (tick several to search them all).
    Saved-but-missing sources stay visible so they aren't silently lost, and a
    pre-v1.4 single-select value becomes the ticked default for that source.
    """
    legacy = str(settings.get(runner.job_field_id(job_name, "epg_source")) or "").strip()
    if legacy.lower() == runner.EPG_SOURCE_NONE:
        legacy = ""

    prefix = f"{runner.job_field_id(job_name, runner.EPG_SOURCE_TOGGLE)}:"
    saved_sources = [k[len(prefix):] for k in settings if k.startswith(prefix)]
    extra = [s for s in dict.fromkeys(saved_sources + ([legacy] if legacy else []))
             if s and s not in active_sources]

    fields = []
    for src in list(active_sources) + extra:
        label = f"Search EPG source: {src}"
        if src in extra:
            label += " (not found in EPG Manager)"
        fields.append({
            "id": f"{prefix}{src}",
            "label": label,
            "type": "boolean",
            "default": src == legacy,
            "help_text": ("Tick one or more sources; their programmes are read from "
                          "the database and searched together. Takes precedence over "
                          "the XMLTV URL below."
                          if src == (active_sources[0] if active_sources else src) else ""),
        })
    return fields


def _build_fields() -> list:
    """Global fields plus one generated group of fields per job."""
    settings = _saved_settings()
    seeds = runner.load_seed_jobs()
    active_sources = _active_epg_source_names()

    try:
        names = runner.parse_job_names(settings, seeds)
    except runner.JobConfigError:
        names = list(seeds.keys())

    fields = []
    for f in GLOBAL_FIELDS:
        f = dict(f)
        if f["id"] == runner.JOBS_LIST_KEY:
            f["default"] = ", ".join(names)
        fields.append(f)

    for name in names:
        fields.append({
            "id": f"job_header:{name}",
            "label": f"⚙ Job: {name}",
            "type": "info",
            "description": f"Settings for the '{name}' job.",
        })
        for key, ui_type, label, help_text in runner.JOB_FIELD_SPECS:
            if key == "xmltv_url":
                # EPG-source checkboxes sit directly above the XMLTV URL.
                fields.extend(_epg_source_toggle_fields(name, settings, active_sources))
            field = {
                "id": runner.job_field_id(name, key),
                "label": label,
                "type": "text" if ui_type == "lines" else ui_type,
                "default": runner.job_ui_default(seeds, name, key),
            }
            if help_text:
                field["help_text"] = help_text
            if key == "purge_mode":
                field["options"] = runner.PURGE_MODE_OPTIONS
            fields.append(field)

    return fields


def _touch_reload_token() -> None:
    """Bump the loader's shared reload token so the next plugin-list fetch
    re-imports this plugin and regenerates the per-job fields."""
    plugins_dir = os.environ.get("DISPATCHARR_PLUGINS_DIR", "/data/plugins")
    token_path = os.path.join(plugins_dir, ".reload_token")
    with open(token_path, "a", encoding="utf-8"):
        pass
    os.utime(token_path, None)


class Plugin:
    name = "Sports Event Auto-Creator"
    version = "1.1.5"
    description = (
        "Auto-creates event channels for sports (boxing, MotoGP, Tennis, ...) from "
        "EPG and stream-name searches, with per-sport jobs and a configurable schedule."
    )
    author = "titooo7"

    actions = [
        {
            "id": "status",
            "label": "Show status",
            "description": "Shows the schedule state, when it last fired, and the result of the last run.",
            "button_label": "Status",
            "button_variant": "subtle",
            "button_color": "cyan",
        },
        {
            "id": "run_now",
            "label": "Run all jobs now",
            "description": "Queues an immediate run of all enabled jobs (also applies the schedule).",
            "button_label": "Run now",
            "button_variant": "filled",
            "button_color": "blue",
            "confirm": {
                "required": True,
                "title": "Run all jobs?",
                "message": "This will delete and recreate event channels in the target groups.",
            },
        },
        {
            "id": "run_single",
            "label": "Run one job",
            "description": "Runs only the job named in the 'Job name' setting.",
            "button_label": "Run job",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "dry_run",
            "label": "Dry run (preview)",
            "description": "Runs all enabled jobs without changing anything; results in the logs.",
            "button_label": "Dry run",
            "button_variant": "outline",
            "button_color": "gray",
        },
        {
            "id": "validate_config",
            "label": "Validate configuration",
            "description": "Checks all job settings for errors without running anything.",
            "button_label": "Validate",
            "button_variant": "subtle",
            "button_color": "teal",
        },
        {
            "id": "export_config",
            "label": "Export jobs JSON",
            "description": "Fills the 'Jobs JSON' box with the current configuration (refresh the page, then copy it). Also writes a file in the plugin folder.",
            "button_label": "Export",
            "button_variant": "subtle",
            "button_color": "green",
        },
        {
            "id": "import_config",
            "label": "Import jobs JSON",
            "description": "Replaces all job settings with the JSON pasted in the 'Jobs JSON' box (press Save first).",
            "button_label": "Import",
            "button_variant": "subtle",
            "button_color": "green",
            "confirm": {
                "required": True,
                "title": "Import jobs configuration?",
                "message": "This replaces ALL current job settings with the pasted JSON. Global settings (schedule, timezone) are kept.",
            },
        },
        {
            "id": "reload_fields",
            "label": "Reload job fields",
            "description": "Rebuilds the per-job settings fields after changing 'Job names'. Refresh the page afterwards.",
            "button_label": "Reload fields",
            "button_variant": "outline",
            "button_color": "grape",
        },
        {
            "id": "apply_schedule",
            "label": "Apply schedule",
            "description": "Creates/updates the background schedule from the settings above.",
            "button_label": "Apply schedule",
            "button_variant": "outline",
            "button_color": "orange",
        },
    ]

    def __init__(self):
        # Evaluated on every plugin (re)load; regenerates per-job fields
        # from the saved "Job names" setting.
        self.fields = _build_fields()

    def run(self, action: str, params: dict, context: dict):
        settings = context.get("settings", {}) or {}
        logger = context.get("logger") or tasks.logger

        if action == "status":
            return {"status": "ok", "message": self._status_message(settings)}

        if action == "validate_config":
            return self._validate(settings)

        if action == "export_config":
            return self._export_config(settings)

        if action == "import_config":
            return self._import_config(settings)

        if action == "reload_fields":
            try:
                names = runner.parse_job_names(settings)
            except runner.JobConfigError as e:
                return {"status": "error", "message": str(e)}
            try:
                _touch_reload_token()
            except Exception as e:
                return {"status": "error", "message": f"Could not trigger a reload: {e}"}
            return {
                "status": "ok",
                "message": (
                    f"Job fields rebuilt for: {', '.join(names) or '(none)'}. "
                    f"Refresh this page to see the updated settings."
                ),
            }

        if action == "apply_schedule":
            try:
                desc = tasks.sync_schedule(settings)
            except Exception as e:
                return {"status": "error", "message": f"Could not apply schedule: {e}"}
            return {"status": "ok", "message": f"Schedule applied: {desc}"}

        if action in ("run_now", "dry_run", "run_single"):
            jobs, errors = runner.jobs_from_settings(settings)
            if errors and not jobs:
                return {"status": "error", "message": " | ".join(errors)}

            job_name = ""
            if action == "run_single":
                job_name = (settings.get("job_filter") or "").strip()
                if not job_name:
                    return {"status": "error",
                            "message": "Set the 'Job name' setting before using 'Run one job'."}
                matched = next((j for j in jobs if j.name.lower() == job_name.lower()), None)
                if matched is None:
                    return {"status": "error",
                            "message": f"No valid job named '{job_name}' found."}
                if not matched.enabled:
                    return {"status": "error",
                            "message": f"Job '{matched.name}' is disabled and will not run. "
                                       f"Tick its 'Enabled' field and press Save first."}

            if action == "run_now":
                try:
                    desc = tasks.sync_schedule(settings)
                    logger.info("Schedule applied: %s", desc)
                except Exception:
                    logger.exception("Could not apply schedule (run continues)")

            # Make sure the workers can actually dispatch our task (they may
            # have built their dispatch table before the plugin was imported).
            replies = tasks.broadcast_strategy_refresh(wait=True)
            worker_ready = any(
                isinstance(reply, dict) and "ok" in reply
                for entry in (replies or []) if isinstance(entry, dict)
                for reply in entry.values()
            )

            dry_run = action == "dry_run"
            tasks.run_jobs_task.delay(job_name=job_name, dry_run=dry_run)
            what = f"job '{job_name}'" if job_name else "all enabled jobs"
            mode = "Dry run" if dry_run else "Run"
            note = f" Warning: {' | '.join(errors)}" if errors else ""
            if not worker_ready:
                note += (" ⚠ No Celery worker confirmed it can run this task — if "
                         "nothing happens, restart the celery container "
                         "(docker restart dispatcharr_celery).")
            return {
                "status": "queued",
                "message": f"{mode} of {what} queued — progress and results appear "
                           f"in the Dispatcharr logs and as a notification when done.{note}",
            }

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context: dict):
        """Called when the plugin is disabled, deleted, or reloaded.

        Only touch the schedule for a real disable/delete. Plugin reloads
        (the refresh icon on the Plugins page, discovery after an update)
        also call stop() — the schedule must survive those, otherwise every
        page refresh silently turns scheduling off.
        """
        reason = (context or {}).get("reason", "")
        if reason == "disable":
            tasks.disable_schedule()
        elif reason == "delete":
            tasks.delete_schedule()
        # reason == "reload" (or unknown): keep the schedule; the task itself
        # no-ops when the plugin is disabled.

    # ------------------------------------------------------------------

    def _export_config(self, settings: dict) -> dict:
        import json

        jobs, errors = runner.jobs_from_settings(settings)
        if errors:
            return {"status": "error",
                    "message": "Cannot export while the config has errors: " + " | ".join(errors)}
        data = [runner.job_to_dict(j) for j in jobs]
        text = json.dumps(data, ensure_ascii=False, indent=2)

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.export.json")
        file_note = ""
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            file_note = path
        except Exception:
            pass

        # Put the JSON into the transfer box so it can be copied from the UI.
        try:
            from apps.plugins.models import PluginConfig
            cfg = PluginConfig.objects.get(key=tasks.PLUGIN_KEY)
            stored = dict(cfg.settings or {})
            stored["transfer_json"] = text
            stored.pop("import_json", None)
            cfg.settings = stored
            cfg.save(update_fields=["settings", "updated_at"])
        except Exception as e:
            if not file_note:
                return {"status": "error", "message": f"Export failed: {e}"}
            return {"status": "ok", "file": file_note,
                    "message": f"Exported {len(jobs)} job(s) to {file_note} "
                               f"(could not fill the UI box: {e})."}

        result = {
            "status": "ok",
            "message": (
                f"Exported {len(jobs)} job(s). REFRESH this page (without pressing "
                f"Save first), open Settings, and copy the JSON from the "
                f"'Jobs JSON (import / export box)' field."
            ),
        }
        if file_note:
            result["file"] = file_note
        return result

    def _import_config(self, settings: dict) -> dict:
        text = (settings.get("transfer_json") or settings.get("import_json") or "").strip()
        if not text:
            return {"status": "error",
                    "message": "Paste the jobs JSON into the 'Jobs JSON (import / export "
                               "box)' field and press Save first."}
        try:
            jobs = runner.parse_jobs(text)
        except runner.JobConfigError as e:
            return {"status": "error", "message": f"Import rejected: {e}"}
        if not jobs:
            return {"status": "error", "message": "Import rejected: the JSON contains no jobs."}

        updates = runner.settings_updates_from_jobs(jobs)
        try:
            from apps.plugins.models import PluginConfig
            cfg = PluginConfig.objects.get(key=tasks.PLUGIN_KEY)
            # Keep global settings; replace all per-job keys with the import.
            stored = {k: v for k, v in dict(cfg.settings or {}).items()
                      if not k.startswith("job:")}
            stored.update(updates)
            stored["transfer_json"] = ""
            stored.pop("import_json", None)
            cfg.settings = stored
            cfg.save(update_fields=["settings", "updated_at"])
        except Exception as e:
            return {"status": "error", "message": f"Could not save imported settings: {e}"}

        try:
            _touch_reload_token()
        except Exception:
            pass
        return {
            "status": "ok",
            "message": (
                f"Imported {len(jobs)} job(s): {', '.join(j.name for j in jobs)}. "
                f"Refresh this page NOW to load the new fields — do not press Save "
                f"before refreshing, or the old form values will overwrite the import."
            ),
        }

    def _status_message(self, settings: dict) -> str:
        from datetime import datetime, timezone

        parts = []

        # Schedule state (from the actual beat table, not just the settings)
        beat_last_run = None
        try:
            from django_celery_beat.models import PeriodicTask
            pt = PeriodicTask.objects.filter(name=tasks.PERIODIC_TASK_NAME).first()
            if pt is None:
                parts.append(
                    "Schedule: NOT CREATED yet — press 'Apply schedule' (or 'Run now')."
                )
            else:
                sched = str(pt.crontab) if pt.crontab else f"every {pt.interval}" if pt.interval else "?"
                state = "enabled" if pt.enabled else "DISABLED"
                beat_last_run = pt.last_run_at
                fired = (pt.last_run_at.strftime("%Y-%m-%d %H:%M UTC")
                         if pt.last_run_at else "never")
                parts.append(f"Schedule: {state}, {sched}; beat last dispatched: {fired} "
                             f"(total {pt.total_run_count}).")
        except Exception as e:
            parts.append(f"Schedule: could not read beat table ({e}).")

        # Last actual execution (written by the Celery task)
        last = tasks.read_last_run()
        last_finished = None
        if last:
            try:
                last_finished = datetime.fromisoformat(last.get("finished_at", ""))
            except ValueError:
                last_finished = None
            when = last.get("finished_at", "?")
            desc = last.get("summary") or last.get("reason") or "; ".join(last.get("errors", [])) or last.get("status", "?")
            parts.append(f"Last run finished {when}: {desc}")
        else:
            parts.append("Last run: none recorded — the Celery worker has never executed the task.")

        # Diagnosis: beat dispatched but the worker never ran it
        try:
            stale = (
                beat_last_run is not None
                and (last_finished is None
                     or (beat_last_run - last_finished).total_seconds() > 300)
                and (datetime.now(timezone.utc) - beat_last_run).total_seconds() > 120
            )
            if stale:
                parts.append(
                    "⚠ Beat dispatched the task but no execution was recorded — the "
                    "Celery worker most likely has the task unregistered. Restart the "
                    "celery container (docker restart dispatcharr_celery) and check its "
                    "log for 'Received unregistered task'."
                )
        except Exception:
            pass

        parts.append(f"Configured schedule setting: {tasks.describe_schedule(settings)}.")
        return " | ".join(parts)

    def _validate(self, settings: dict) -> dict:
        jobs, errors = runner.jobs_from_settings(settings)
        if errors:
            return {"status": "error", "message": " | ".join(errors)}

        tz_name = (settings.get("display_timezone") or "Europe/Madrid").strip()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz_name)
        except Exception:
            return {"status": "error", "message": f"Invalid display timezone: '{tz_name}'"}

        cron = (settings.get("cron_expression") or "").strip()
        if cron:
            parts = cron.split()
            if len(parts) != 5:
                return {"status": "error",
                        "message": "Cron expression must have 5 parts (minute hour day month weekday)."}
            try:
                from celery.schedules import crontab
                crontab(minute=parts[0], hour=parts[1], day_of_month=parts[2],
                        month_of_year=parts[3], day_of_week=parts[4])
            except Exception as e:
                return {"status": "error",
                        "message": f"Invalid cron expression '{cron}': {e}"}

        enabled = [j.name for j in jobs if j.enabled]
        disabled = [j.name for j in jobs if not j.enabled]
        msg = f"Config OK — {len(jobs)} job(s): {', '.join(enabled) or 'none'} enabled"
        if disabled:
            msg += f"; disabled: {', '.join(disabled)}"
        msg += f". Schedule: {tasks.describe_schedule(settings)}."
        return {"status": "ok", "message": msg}
