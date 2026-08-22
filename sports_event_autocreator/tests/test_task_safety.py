"""
Unit tests for tasks.py's run-safety net: the top-level crash handler,
last_success_at tracking, and the teamarr-watcher/retention error counting.

tasks.py imports `celery` at module level (fine -- installed in the test
venv alongside pytest) but also needs `django.db.close_old_connections` and
`django.core.cache.cache` INSIDE run_jobs_task's body. Rather than installing
real Django (which would then also need the actual Dispatcharr `apps.*`
package tree to do anything useful), this file stubs minimal fake modules
into sys.modules for exactly those two import points, and monkeypatches
tasks.py's own module-level helpers (`_get_plugin_config`, `runner.
jobs_from_settings`, `runner.run_job`, `runner.run_teamarr_watch`,
`runner.run_retention`, `runner.load_auto_dvr_index`) so the task body runs
end-to-end without a real Django/Dispatcharr environment.

Run:  python3 -m pytest sports_event_autocreator/tests/test_task_safety.py
      (from the repo root, using the venv set up for this task:
       python3 -m venv .venv && .venv/bin/pip install pytest celery requests
       .venv/bin/python -m pytest sports_event_autocreator/tests/ -q)
"""

import os
import sys
import types
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _FakeCache:
    """Minimal stand-in for django.core.cache.cache: just enough of the
    add()/delete() contract the overlap-lock guard uses."""
    def __init__(self):
        self._store = {}
        self.force_add_result = None  # None = real add() semantics

    def add(self, key, value, timeout=None):
        if self.force_add_result is not None:
            return self.force_add_result
        if key in self._store:
            return False
        self._store[key] = value
        return True

    def delete(self, key):
        self._store.pop(key, None)


def _install_django_stubs():
    """Seed sys.modules with fake django.db / django.core(.cache) modules,
    only if real Django isn't already importable -- so this stays correct
    if this suite is ever run in an environment that DOES have Django."""
    try:
        import django  # noqa: F401
        return None  # real Django is present; nothing to stub
    except ImportError:
        pass

    django_pkg = types.ModuleType("django")
    django_pkg.__path__ = []  # mark as a package
    sys.modules.setdefault("django", django_pkg)

    django_db = types.ModuleType("django.db")
    django_db.close_old_connections = lambda: None
    sys.modules["django.db"] = django_db

    django_core = types.ModuleType("django.core")
    django_core.__path__ = []
    sys.modules.setdefault("django.core", django_core)

    django_core_cache = types.ModuleType("django.core.cache")
    fake_cache = _FakeCache()
    django_core_cache.cache = fake_cache
    sys.modules["django.core.cache"] = django_core_cache
    return fake_cache


_FAKE_CACHE = _install_django_stubs()

from sports_event_autocreator import tasks  # noqa: E402
from sports_event_autocreator import runner  # noqa: E402


def _fake_job(name="Test Job", enabled=True):
    return SimpleNamespace(name=name, enabled=enabled)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("DISPATCHARR_PLUGINS_DIR", str(tmp_path))
    if _FAKE_CACHE is not None:
        _FAKE_CACHE._store.clear()
        _FAKE_CACHE.force_add_result = None
    return tmp_path


@pytest.fixture
def stub_plugin_config(monkeypatch):
    cfg = SimpleNamespace(enabled=True, settings={})
    monkeypatch.setattr(tasks, "_get_plugin_config", lambda: cfg)
    return cfg


@pytest.fixture
def stub_run_job_pipeline(monkeypatch):
    """Baseline happy-path stand-ins for everything run_jobs_task calls into
    runner for, so tests can override just the one thing they're exercising."""
    monkeypatch.setattr(runner, "_recording_model", lambda: object())
    monkeypatch.setattr(runner, "load_auto_dvr_index", lambda model: [])
    monkeypatch.setattr(runner, "run_job", lambda *a, **k: {
        "prepared": 0, "created": 0, "deleted": 0, "skipped": 0,
        "preserved": 0, "errors": 0, "recorded": 0, "capped": 0,
        "extended": 0, "rescheduled": 0, "epg_scanned": 0, "epg_matched": 0,
    })
    monkeypatch.setattr(runner, "run_teamarr_watch", lambda *a, **k: {
        "created": 0, "skipped": 0, "errors": 0,
        "extended": 0, "rescheduled": 0, "capped": 0,
    })
    monkeypatch.setattr(runner, "run_retention", lambda *a, **k: {"deleted": 0, "errors": 0})
    monkeypatch.setattr(runner, "prune_dvr_tombstones", lambda logger: 0)


@pytest.fixture
def notify_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "_notify", lambda message, success=True, **k:
                        calls.append({"message": message, "success": success}))
    return calls


def _run_task():
    """Invoke the celery Task's underlying function synchronously (no
    broker/worker needed -- calling a @shared_task-decorated object executes
    its wrapped function directly)."""
    return tasks.run_jobs_task()


# ------------------------------- crash handling -----------------------------

def test_crash_writes_crashed_status_and_notifies(stub_plugin_config, notify_calls, monkeypatch):
    def _boom(settings):
        raise RuntimeError("boom: simulated orchestration bug")
    monkeypatch.setattr(runner, "jobs_from_settings", _boom)

    result = _run_task()
    assert result["status"] == "crashed"

    last = tasks.read_last_run()
    assert last is not None
    assert last["status"] == "crashed"
    assert "boom" in last["error"]

    assert any(not c["success"] for c in notify_calls)
    assert any("CRASHED" in c["message"] for c in notify_calls)


@pytest.mark.skipif(_FAKE_CACHE is None,
                    reason="real Django is installed; won't monkeypatch its close_old_connections")
def test_broken_import_still_crashes_cleanly_no_nameerror(
        stub_plugin_config, notify_calls, monkeypatch):
    """Fix 5 regression test: run_jobs_task's `from django.db import
    close_old_connections` / `from django.core.cache import cache` / `from .
    import engine, runner` used to sit OUTSIDE the top-level try/except. If
    any of them failed to import (e.g. right after a bad plugin update --
    exactly the scenario the crash-safety net exists to catch), the
    exception escaped the handler entirely (no last_run.json write, no
    notification) AND the `finally` block then raised a SECOND, unrelated
    NameError referencing an unbound `cache`. Simulates that exact failure
    by breaking the stubbed django.db module's close_old_connections, and
    asserts the task still returns cleanly with a proper crash report."""
    import django.db as fake_django_db
    monkeypatch.delattr(fake_django_db, "close_old_connections", raising=False)

    result = _run_task()  # must not raise -- NameError included

    assert result["status"] == "crashed"
    last = tasks.read_last_run()
    assert last is not None
    assert last["status"] == "crashed"
    assert "close_old_connections" in last["error"] or "ImportError" in last["error"] \
        or "cannot import name" in last["error"]

    assert any(not c["success"] for c in notify_calls)
    assert any("CRASHED" in c["message"] for c in notify_calls)


def test_crash_preserves_prior_last_success_at(stub_plugin_config, notify_calls, monkeypatch):
    # Seed a prior genuine success.
    tasks._write_last_run({"status": "ok", "summary": "3 created"}, mark_success=True)
    prior = tasks.read_last_run()
    assert prior["last_success_at"]

    def _boom(settings):
        raise RuntimeError("boom again")
    monkeypatch.setattr(runner, "jobs_from_settings", _boom)

    _run_task()

    after = tasks.read_last_run()
    assert after["status"] == "crashed"
    assert after["last_success_at"] == prior["last_success_at"]
    assert after["last_success_summary"] == prior["last_success_summary"]


# ------------------------------ overlap lock skip ---------------------------

def test_overlap_lock_skip_does_not_clobber_last_success_at(stub_plugin_config, notify_calls):
    tasks._write_last_run({"status": "ok", "summary": "5 created"}, mark_success=True)
    prior = tasks.read_last_run()
    assert prior["last_success_at"]

    if _FAKE_CACHE is not None:
        _FAKE_CACHE.force_add_result = False  # simulate: another run holds the lock

    result = _run_task()
    assert result["status"] == "skipped"

    after = tasks.read_last_run()
    assert after["status"] == "skipped"
    assert after["last_success_at"] == prior["last_success_at"]


# --------------------------- subsystem failure counting ---------------------

def test_teamarr_watcher_exception_counts_as_error_and_fails_run(
        stub_plugin_config, stub_run_job_pipeline, notify_calls, monkeypatch):
    monkeypatch.setattr(runner, "jobs_from_settings", lambda settings: ([_fake_job()], []))

    def _boom(*a, **k):
        raise RuntimeError("teamarr blew up")
    monkeypatch.setattr(runner, "run_teamarr_watch", _boom)

    result = _run_task()
    assert result["status"] == "ok"  # the task itself didn't crash...
    assert any(not c["success"] for c in notify_calls)  # ...but the run is reported as failed

    last = tasks.read_last_run()
    assert last["jobs"].get("__teamarr__", {}).get("error")
    assert "teamarr blew up" in last["jobs"]["__teamarr__"]["error"]
    # A run with a subsystem failure must never be recorded as the last success.
    assert last.get("last_success_at") is None


def test_retention_exception_counts_as_error_and_fails_run(
        stub_plugin_config, stub_run_job_pipeline, notify_calls, monkeypatch):
    monkeypatch.setattr(runner, "jobs_from_settings", lambda settings: ([_fake_job()], []))

    def _boom(*a, **k):
        raise RuntimeError("retention blew up")
    monkeypatch.setattr(runner, "run_retention", _boom)

    result = _run_task()
    assert result["status"] == "ok"
    assert any(not c["success"] for c in notify_calls)

    last = tasks.read_last_run()
    assert last["jobs"].get("__retention__", {}).get("error")
    assert "retention blew up" in last["jobs"]["__retention__"]["error"]
    assert last.get("last_success_at") is None


def test_genuinely_successful_run_marks_last_success_at(
        stub_plugin_config, stub_run_job_pipeline, notify_calls, monkeypatch):
    monkeypatch.setattr(runner, "jobs_from_settings", lambda settings: ([_fake_job()], []))

    result = _run_task()
    assert result["status"] == "ok"
    assert all(c["success"] for c in notify_calls)

    last = tasks.read_last_run()
    assert last.get("last_success_at")
    assert last.get("last_success_summary")


# --------------------- v1.3.0 new stats keys survive totals fold-in ---------
# Regression test for the exact class of bug v1.2.0 had to fix once already
# (a key missing from totals's initial dict gets silently dropped when
# folding per-job stats in): every new v1.3.0 stats key must both be present
# in totals's initialization AND actually accumulate from a job's stats dict,
# with no KeyError anywhere in the fold-in loop.

_NEW_STATS_KEYS = ("probes", "probe_capped", "unnumbered", "number_conflicts",
                   "unowned_kept", "adopted", "updated", "update_deferred")


def test_new_v1_3_0_stats_keys_survive_totals_fold_in(
        stub_plugin_config, notify_calls, monkeypatch):
    monkeypatch.setattr(runner, "jobs_from_settings", lambda settings: ([_fake_job()], []))
    monkeypatch.setattr(runner, "_recording_model", lambda: object())
    monkeypatch.setattr(runner, "load_auto_dvr_index", lambda model: [])
    monkeypatch.setattr(runner, "run_teamarr_watch", lambda *a, **k: {
        "created": 0, "skipped": 0, "errors": 0,
        "extended": 0, "rescheduled": 0, "capped": 0,
    })
    monkeypatch.setattr(runner, "run_retention", lambda *a, **k: {"deleted": 0, "errors": 0})
    monkeypatch.setattr(runner, "prune_dvr_tombstones", lambda logger: 0)

    per_job_stats = {
        "prepared": 1, "created": 1, "deleted": 0, "skipped": 0,
        "preserved": 0, "errors": 0, "recorded": 0, "capped": 0,
        "extended": 0, "rescheduled": 0, "epg_scanned": 0, "epg_matched": 0,
        "probes": 3, "probe_capped": 1, "unnumbered": 2, "number_conflicts": 4,
        "unowned_kept": 5, "adopted": 6, "updated": 7, "update_deferred": 8,
    }
    monkeypatch.setattr(runner, "run_job", lambda *a, **k: dict(per_job_stats))

    result = _run_task()  # must not raise KeyError anywhere in the fold-in loop
    assert result["status"] == "ok"

    last = tasks.read_last_run()
    job_stats = last["jobs"]["Test Job"]
    for key in _NEW_STATS_KEYS:
        assert job_stats[key] == per_job_stats[key]

    summary = last["summary"]
    for key in _NEW_STATS_KEYS:
        assert per_job_stats[key] > 0  # sanity: every key actually exercised
    # The summary string must reflect the new counters, not silently drop them.
    assert "3 black-screen probe" in summary
    assert "2 unnumbered" in summary
    assert "4 number conflict" in summary
    assert "6 channel(s) adopted" in summary
    assert "5 unowned channel(s) kept" in summary
    assert "7 updated in place" in summary
    assert "8 stream-swap deferred" in summary
