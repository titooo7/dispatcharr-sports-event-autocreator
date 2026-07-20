"""
Celery task and beat-schedule management for the Sports Event Auto-Creator.

The @shared_task below is registered when this module is imported. Dispatcharr
imports enabled plugin modules in the web workers (app ready) and, per-process,
in every Celery worker process — but WHICH signal triggers that varies by
Dispatcharr version and, critically, only reliably covers `--pool=threads`
workers (no parent/child split) or prefork CHILD processes, never a prefork
ARBITER (the process that validates/dispatches incoming messages). Routing
this task to the `dvr` queue (a `--pool=threads` worker on every standard
Dispatcharr install) sidesteps that entirely — see the long comment further
down, right above the STRATEGY_REFRESH_COMMAND/queue="dvr" combo, for the
full story of why a plugin-side signal hook cannot fix the arbiter case.

The user-selected run frequency (interval_minutes or cron_expression settings)
is materialized as a django_celery_beat PeriodicTask that calls this task.
"""

import json
import logging
import os
from datetime import datetime, timezone

from celery import shared_task

PLUGIN_KEY = "sports_event_autocreator"
TASK_NAME = f"{PLUGIN_KEY}.run_jobs"
PERIODIC_TASK_NAME = "sports-event-autocreator-run-jobs"

# Written after every task execution; read by the 'Show status' action.
# Lives next to the plugin code (inside /data) so it survives UI settings
# saves, which replace the settings dict wholesale.
LAST_RUN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run.json")

logger = logging.getLogger(f"plugins.{PLUGIN_KEY}")

# ---------------------------------------------------------------------------
# Workaround for a Dispatcharr/celery interaction bug:
#
# Dispatcharr imports plugin modules in the celery worker via the
# `worker_ready` signal, which fires AFTER the consumer's Tasks bootstep has
# already built its task-dispatch table (`update_strategies()`). Tasks
# registered that late show up in `inspect registered` but every delivery is
# discarded with "Received unregistered task ... KeyError" — celery never
# re-checks the registry (verified against celery 5.6.3 source).
#
# Remote control commands, however, ARE dispatched dynamically. So we
# register one that rebuilds the dispatch table, and broadcast it right
# after import inside a worker process: by the time the worker's event loop
# starts consuming, the command is waiting and heals the table.
#
# Second half of the same bug: with a prefork pool the child processes are
# forked BEFORE worker_ready fires, so a child forked at boot never imports
# the plugin at all — the (healed) parent accepts a delivery, hands it to
# that child, and the child raises NotRegistered. Children forked later
# (autoscale scaling up under load) inherit the import and work, which makes
# the failure look random. The refresh command therefore also recycles those
# pre-plugin children: each one is replaced via terminate_controlled() — the
# same graceful mechanism pool shrink / autoscale uses — and the pool
# maintainer respawns a fresh child that inherits the plugin import. Busy
# children are skipped and retried on the next refresh (one runs before every
# queued run). Note pool.restart() can NOT be used for this: celery only
# creates billiard's restart sentinels when worker_pool_restarts is enabled
# (Dispatcharr doesn't), so restart() crashes on the None sentinel.
# Thread/solo pools run tasks in the main process — already healed — and are
# detected and skipped.
# ---------------------------------------------------------------------------

STRATEGY_REFRESH_COMMAND = "sea_refresh_strategies"

# pids of pool children forked before this plugin module was imported (they
# cannot execute the plugin task). Snapshotted on the first refresh after
# import; shrinks as children get recycled, then stays empty.
_stale_child_pids = None


def _recycle_stale_children(state) -> None:
    global _stale_child_pids
    raw_pool = getattr(getattr(state.consumer, "pool", None), "_pool", None)
    if raw_pool is None or not hasattr(raw_pool, "_worker_active"):
        return  # threads/solo pool: tasks run in the (healed) main process
    procs = [p for p in list(getattr(raw_pool, "_pool", []) or []) if p.pid]
    if _stale_child_pids is None:
        # First refresh runs right after the worker_ready import, so every
        # child alive now was forked before the plugin existed in the parent.
        _stale_child_pids = {p.pid for p in procs}
    remaining = [p for p in procs if p.pid in _stale_child_pids]
    _stale_child_pids &= {p.pid for p in remaining}  # forget already-exited pids
    if not remaining:
        return
    recycled = set()
    for proc in remaining:
        try:
            if raw_pool._worker_active(proc):
                continue  # mid-task; picked up again on the next refresh
            proc.terminate_controlled()
            recycled.add(proc.pid)
        except Exception:
            logger.debug("Could not recycle pool child %s", proc.pid, exc_info=True)
    _stale_child_pids -= recycled
    if recycled:
        logger.info(
            "Recycled %d pre-plugin pool child(ren); replacements will register "
            "the plugin task%s", len(recycled),
            f" ({len(_stale_child_pids)} busy, retrying on next refresh)"
            if _stale_child_pids else "")


try:
    from celery.worker.control import control_command

    @control_command()
    def sea_refresh_strategies(state):
        """Rebuild the consumer's task-dispatch table so tasks registered
        after consumer start (plugins imported at worker_ready) become
        deliverable without a broker reconnect, and recycle pool children
        forked before the plugin import (they can't run the task)."""
        state.consumer.update_strategies()
        logger.info("Task dispatch table refreshed (%s)", STRATEGY_REFRESH_COMMAND)
        try:
            _recycle_stale_children(state)
        except Exception:
            logger.warning(
                "Could not recycle pre-plugin pool children — a run may still "
                "hit one (NotRegistered); it heals on later refreshes or a "
                "container restart", exc_info=True)
        return {"ok": "strategies refreshed"}
except Exception:
    logger.debug("Could not register %s control command", STRATEGY_REFRESH_COMMAND,
                 exc_info=True)


# ---------------------------------------------------------------------------
# Dispatcharr >= 0.28.0: the ARBITER (prefork parent / consumer) process no
# longer imports plugin modules at all. Discovery moved from `worker_ready`
# (fires once, in the arbiter) to `worker_process_init` (fires per prefork
# CHILD, deliberately skipping the parent so it never opens DB connections
# autoscale children would inherit via fork — see dispatcharr/celery.py).
#
# For a `--pool=threads` worker (this Dispatcharr's `dvr` queue) there is no
# separate child process, so that single process is both "arbiter" and
# "worker" and gets the plugin import either way. But for a
# `--pool=prefork --autoscale=...` worker (the `celery`/default queue this
# task was previously routed to), the ARBITER (parent) process never imports
# any plugin module at all, by design — only its prefork children do, on
# their own worker_process_init. The arbiter is the process that actually
# validates/dispatches every incoming message (children only execute work
# already handed to them), so nothing a child does — and no signal hook a
# plugin can add, since hooking a signal itself requires the module to
# already be imported in that same process — can retroactively fix the
# arbiter's registry. Confirmed by direct testing (Dispatcharr 0.28.0): the
# arbiter's own `app.tasks` picks up a manually-triggered import fine, and
# even an explicit `consumer.update_strategies()` call in that same process
# doesn't help, because the fix can never actually run *in* the arbiter in
# the first place — its own `worker_ready` fires before any plugin-supplied
# signal handler could ever be connected there.
#
# Symptom without this fix: "Received unregistered task ... KeyError:
# 'sports_event_autocreator.run_jobs'" from the arbiter's on_task_received,
# and "pidbox command error: KeyError('sea_refresh_strategies')" for the
# same reason — the arbiter's pidbox handler registry never gained the
# control command above either.
#
# Fix: route this task to the `dvr` queue instead — the one queue guaranteed
# to be served by a `--pool=threads` worker (no prefork parent/child split)
# on every Dispatcharr install using the standard entrypoint. Trade-off:
# job-creation runs share the dvr queue's thread pool with real recording
# tasks; at the default `--concurrency=20` and typical run frequency this
# is not a practical concern, but note it if that pool is ever narrowed.


def _get_celery_app():
    try:
        from dispatcharr.celery import app
        return app
    except Exception:
        from celery import current_app
        return current_app


def broadcast_strategy_refresh(wait: bool = False):
    """
    Ask all workers to rebuild their dispatch tables.
    With wait=True, returns the list of worker replies (empty = no worker
    answered, i.e. the workers don't know the command yet and need a restart).
    """
    try:
        app = _get_celery_app()
        if wait:
            return app.control.broadcast(
                STRATEGY_REFRESH_COMMAND, reply=True, timeout=2.0) or []
        app.control.broadcast(STRATEGY_REFRESH_COMMAND)
        return None
    except Exception:
        logger.debug("Strategy-refresh broadcast failed", exc_info=True)
        return [] if wait else None


def _is_celery_worker_process() -> bool:
    import sys
    argv = " ".join(sys.argv)
    return "celery" in argv and ("worker" in argv or "beat" in argv)


# Self-heal on worker boot: this module is imported during worker_ready, so
# queue the refresh now — the event loop starts right after and processes it.
if _is_celery_worker_process():
    broadcast_strategy_refresh()


def _write_last_run(payload: dict) -> None:
    try:
        payload = dict(payload)
        payload["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.debug("Could not write last_run.json", exc_info=True)


def read_last_run() -> dict | None:
    try:
        with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _get_plugin_config():
    from apps.plugins.models import PluginConfig
    return PluginConfig.objects.filter(key=PLUGIN_KEY).first()


# ---------------------------------------------------------------------------
# Beat schedule management
# ---------------------------------------------------------------------------

def describe_schedule(settings: dict) -> str:
    cron = (settings.get("cron_expression") or "").strip()
    if cron:
        return f"cron '{cron}'"
    try:
        minutes = int(float(settings.get("interval_minutes") or 0))
    except (TypeError, ValueError):
        minutes = 0
    if minutes > 0:
        return f"every {minutes} minute(s)"
    return "disabled (interval is 0 and no cron expression set)"


def sync_schedule(settings: dict) -> str:
    """
    Create/update/disable the beat PeriodicTask to match the plugin settings.
    Returns a human-readable description of the resulting schedule.
    """
    from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask
    from core.models import CoreSettings

    cron = (settings.get("cron_expression") or "").strip()
    try:
        minutes = int(float(settings.get("interval_minutes") or 0))
    except (TypeError, ValueError):
        minutes = 0

    existing = PeriodicTask.objects.filter(name=PERIODIC_TASK_NAME).first()
    old_interval = existing.interval if existing else None
    old_crontab = existing.crontab if existing else None

    if cron:
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError("Cron expression must have 5 parts: minute hour day month weekday")
        # Validate the fields with celery before persisting: a bad expression
        # (e.g. hour '8-25') saved to django_celery_beat can crash the beat
        # process, which drives ALL of Dispatcharr's scheduled work.
        try:
            from celery.schedules import crontab as _celery_crontab
            _celery_crontab(minute=parts[0], hour=parts[1], day_of_month=parts[2],
                            month_of_year=parts[3], day_of_week=parts[4])
        except Exception as e:
            raise ValueError(f"Invalid cron expression '{cron}': {e}")
        try:
            system_tz = CoreSettings.get_system_time_zone()
        except Exception:
            system_tz = "UTC"
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute=parts[0], hour=parts[1], day_of_month=parts[2],
            month_of_year=parts[3], day_of_week=parts[4], timezone=system_tz,
        )
        PeriodicTask.objects.update_or_create(
            name=PERIODIC_TASK_NAME,
            defaults={"task": TASK_NAME, "crontab": crontab, "interval": None,
                      "enabled": True, "kwargs": "{}"},
        )
    elif minutes > 0:
        interval, _ = IntervalSchedule.objects.get_or_create(
            every=minutes, period=IntervalSchedule.MINUTES,
        )
        PeriodicTask.objects.update_or_create(
            name=PERIODIC_TASK_NAME,
            defaults={"task": TASK_NAME, "interval": interval, "crontab": None,
                      "enabled": True, "kwargs": "{}"},
        )
    else:
        # Disabled: keep the row but switch it off so beat stops firing.
        PeriodicTask.objects.filter(name=PERIODIC_TASK_NAME).update(enabled=False)

    _cleanup_orphans(old_interval, old_crontab)
    desc = describe_schedule(settings)
    logger.info("Schedule synced: %s", desc)
    return desc


def disable_schedule() -> None:
    """Switch the periodic task off (plugin disabled in the UI)."""
    try:
        from django_celery_beat.models import PeriodicTask
        PeriodicTask.objects.filter(name=PERIODIC_TASK_NAME).update(enabled=False)
        logger.info("Periodic task disabled")
    except Exception:
        logger.exception("Failed to disable periodic task")


def delete_schedule() -> None:
    """Remove the periodic task entirely (plugin deleted), so beat doesn't
    keep dispatching a task that no longer exists."""
    try:
        from django_celery_beat.models import PeriodicTask
        existing = PeriodicTask.objects.filter(name=PERIODIC_TASK_NAME).first()
        if existing:
            old_interval, old_crontab = existing.interval, existing.crontab
            existing.delete()
            _cleanup_orphans(old_interval, old_crontab)
            logger.info("Periodic task deleted")
    except Exception:
        logger.exception("Failed to delete periodic task")


def _cleanup_orphans(old_interval, old_crontab):
    """Delete schedule rows no longer referenced by any PeriodicTask."""
    from django_celery_beat.models import PeriodicTask
    try:
        if old_interval and not PeriodicTask.objects.filter(interval=old_interval).exists():
            old_interval.delete()
        if old_crontab and not PeriodicTask.objects.filter(crontab=old_crontab).exists():
            old_crontab.delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The scheduled/queued task
# ---------------------------------------------------------------------------

@shared_task(name=TASK_NAME, queue="dvr")
def run_jobs_task(job_name: str = "", dry_run: bool = False) -> dict:
    """
    Run all enabled jobs (or a single job when job_name is given).
    Fired by Celery beat on the user-configured schedule and queued by the
    plugin's UI actions via .delay().
    """
    from django.db import close_old_connections
    from django.core.cache import cache
    from . import engine, runner

    # Non-blocking overlap guard: if a run outlasts the beat interval or "Run
    # now" is pressed mid-run, two tasks over the same groups race (duplicate
    # channels, delete/create conflicts). A cache lock lets the second bail out.
    lock_key = f"{PLUGIN_KEY}:run_lock"
    lock_acquired = False
    try:
        try:
            lock_acquired = bool(cache.add(
                lock_key, datetime.now(timezone.utc).isoformat(), timeout=1800))
        except Exception:
            # Cache backend unavailable — never let a lock failure mask the run.
            logger.debug("Run-lock cache.add failed; proceeding without a lock",
                         exc_info=True)
        else:
            if not lock_acquired:
                logger.warning("Another run is already in progress; skipping this run")
                result = {"status": "skipped",
                          "reason": "another run is already in progress"}
                _write_last_run(result)
                return result

        cfg = _get_plugin_config()
        if not cfg or not cfg.enabled:
            logger.info("Plugin is disabled; skipping run")
            _write_last_run({"status": "skipped", "reason": "plugin disabled"})
            return {"status": "skipped", "reason": "plugin disabled"}

        settings = cfg.settings or {}

        # Self-heal: if the user changed the interval/cron in settings but
        # didn't press "Apply schedule", re-sync on this tick.
        try:
            sync_schedule(settings)
        except Exception:
            logger.exception("Schedule self-heal failed (continuing with run)")

        engine.set_display_timezone(settings.get("display_timezone") or "Europe/Madrid")

        jobs, config_errors = runner.jobs_from_settings(settings)
        for err in config_errors:
            logger.error("Invalid job configuration: %s", err)
        if config_errors and not jobs:
            _notify(f"Sports Auto-Creator: invalid configuration — {config_errors[0]}",
                    success=False)
            _write_last_run({"status": "error", "errors": config_errors})
            return {"status": "error", "message": " | ".join(config_errors)}

        if job_name:
            jobs = [j for j in jobs if j.name.lower() == job_name.strip().lower()]
            if not jobs:
                msg = f"No job named '{job_name}' found"
                logger.error(msg)
                _notify(f"Sports Auto-Creator: {msg}", success=False)
                _write_last_run({"status": "error", "errors": [msg]})
                return {"status": "error", "message": msg}

        jobs = [j for j in jobs if j.enabled]
        if not jobs:
            logger.info("No enabled jobs to run")
            _write_last_run({"status": "ok", "summary": "No enabled jobs"})
            return {"status": "ok", "message": "No enabled jobs"}

        mode = "DRY RUN" if dry_run else "run"
        logger.info("Starting %s of %d job(s): %s", mode, len(jobs),
                    ", ".join(j.name for j in jobs))

        assign_epg = bool(settings.get("assign_epg", True))
        use_stream_logo = bool(settings.get("use_stream_logo", True))
        try:
            event_duration_hours = float(settings.get("event_duration_hours") or 3)
        except (TypeError, ValueError):
            event_duration_hours = 3.0
        if event_duration_hours <= 0:
            event_duration_hours = 3.0

        try:
            black_sample_seconds = float(settings.get("black_sample_seconds") or 5)
        except (TypeError, ValueError):
            black_sample_seconds = 5.0
        black_sample_seconds = max(3.0, min(30.0, black_sample_seconds))
        # Shared across every event AND job in this run: caches probe verdicts
        # by stream id and caps how many ffmpeg probes the run performs.
        probe_state = {
            "cache": {},
            "budget": 40,
            "sample_seconds": black_sample_seconds,
            "ffmpeg_missing_logged": False,
        }

        # Auto-DVR (Replays) settings.
        def _num(key, default):
            try:
                return float(settings.get(key))
            except (TypeError, ValueError):
                return float(default)
        record_pre_pad_min = max(0.0, _num("record_pre_pad_min", 5))
        record_post_pad_min = max(0.0, _num("record_post_pad_min", 30))
        retention_days = int(max(0.0, _num("replay_retention_days", 14)))
        max_simultaneous_recordings = int(max(0.0, _num("max_simultaneous_recordings", 2)))

        xmltv_cache = {}
        totals = {"prepared": 0, "created": 0, "deleted": 0,
                  "skipped": 0, "preserved": 0, "errors": 0, "recorded": 0}
        per_job = {}
        for job in jobs:
            try:
                stats = runner.run_job(job, logger, dry_run=dry_run, xmltv_cache=xmltv_cache,
                                       assign_epg=assign_epg,
                                       event_duration_hours=event_duration_hours,
                                       use_stream_logo=use_stream_logo,
                                       probe_state=probe_state,
                                       record_pre_pad_min=record_pre_pad_min,
                                       record_post_pad_min=record_post_pad_min,
                                       max_simultaneous_recordings=max_simultaneous_recordings)
                per_job[job.name] = stats
                for k in totals:
                    totals[k] += stats.get(k, 0)
            except Exception as e:
                logger.exception("Job '%s' failed", job.name)
                per_job[job.name] = {"error": str(e)}
                totals["errors"] += 1

        # Teamarr-group watcher: auto-record matching Teamarr event channels.
        teamarr_stats = None
        try:
            teamarr_stats = runner.run_teamarr_watch(
                runner._as_lines(settings.get("record_teamarr_groups")),
                runner._as_lines(settings.get("record_teamarr_patterns")),
                runner._as_lines(settings.get("record_teamarr_exclude")),
                logger, dry_run, event_duration_hours,
                record_pre_pad_min, record_post_pad_min,
                max_simultaneous=max_simultaneous_recordings)
            totals["recorded"] += teamarr_stats.get("created", 0)
            totals["errors"] += teamarr_stats.get("errors", 0)
        except Exception:
            logger.exception("Teamarr watcher failed")

        # Retention: prune aged / failed auto-DVR recordings (files + rows).
        retention_stats = None
        try:
            retention_stats = runner.run_retention(retention_days, logger, dry_run)
        except Exception:
            logger.exception("Retention pass failed")

        prefix = "[DRY RUN] " if dry_run else ""
        summary = (f"{prefix}Sports Auto-Creator: {totals['created']} created, "
                   f"{totals['deleted']} deleted, {totals['skipped']} skipped, "
                   f"{totals['recorded']} recorded "
                   f"across {len(jobs)} job(s)"
                   + (f", {retention_stats['deleted']} recording(s) pruned"
                      if retention_stats and retention_stats.get("deleted") else "")
                   + (f" — {totals['errors']} error(s)" if totals['errors'] else "")
                   + (f" — {len(config_errors)} misconfigured job(s) skipped"
                      if config_errors else ""))
        logger.info(summary)
        _notify(summary, success=totals["errors"] == 0 and not config_errors,
                refresh_channels=not dry_run and (totals["created"] > 0 or totals["deleted"] > 0))

        _write_last_run({"status": "ok", "dry_run": dry_run, "job_name": job_name,
                         "summary": summary, "jobs": per_job, "errors": config_errors})
        return {"status": "ok", "summary": summary, "jobs": per_job}
    finally:
        close_old_connections()
        if lock_acquired:
            try:
                cache.delete(lock_key)
            except Exception:
                logger.debug("Failed to release run lock", exc_info=True)


def _notify(message: str, success: bool = True, refresh_channels: bool = False) -> None:
    """Best-effort websocket toast so results are visible in the UI."""
    try:
        from core.utils import send_websocket_update
        send_websocket_update(
            "updates",
            "update",
            {
                "success": success,
                "type": "plugin",
                "plugin": PLUGIN_KEY,
                "refresh_channels": refresh_channels,
                "message": message,
            },
        )
    except Exception:
        logger.debug("Websocket notify failed", exc_info=True)
