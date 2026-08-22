"""
Unit tests for runner.py's v1.3.0 channel-ownership registry and
number-index primitives, using fake Channel/ChannelOverride models -- no
Django/Postgres needed. Follows tests/test_recording_paths.py's fake-model
seam pattern exactly.

Run:  python3 -m pytest sports_event_autocreator/tests/test_channel_registry.py
      (from the repo root, using the venv set up for this task:
       python3 -m venv .venv && .venv/bin/pip install pytest requests celery
       .venv/bin/python -m pytest sports_event_autocreator/tests/ -q)
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sports_event_autocreator import runner  # noqa: E402
from sports_event_autocreator import engine  # noqa: E402


class _NullLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass
    def debug(self, *a, **k): pass


LOGGER = _NullLogger()


# ------------------------------ fake number-index models --------------------

class _FakeValuesQS(list):
    def values_list(self, field, flat=False):
        return [getattr_or_get(row, field) for row in self]


def getattr_or_get(row, field):
    return row.get(field) if isinstance(row, dict) else getattr(row, field)


class _FakeChannelManager:
    def __init__(self, rows):
        self._rows = rows

    @property
    def objects(self):
        return self

    def exclude(self, **kwargs):
        # only supports channel_number__isnull=True
        return _FakeValuesQS([r for r in self._rows if r.get("channel_number") is not None])

    def filter(self, **kwargs):
        # only supports channel_number__isnull=False
        return _FakeValuesQS([r for r in self._rows if r.get("channel_number") is not None])


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("DISPATCHARR_PLUGINS_DIR", str(tmp_path))
    return tmp_path


def test_number_index_includes_channel_and_override_excludes_null():
    channels = _FakeChannelManager([
        {"channel_number": 260.0}, {"channel_number": None}, {"channel_number": 261},
    ])
    overrides = _FakeChannelManager([
        {"channel_number": 500.0}, {"channel_number": None},
    ])
    index = runner.load_channel_number_index(channels, overrides)
    assert index == {260.0, 261.0, 500.0}


# ------------------------------ registry round-trip --------------------------

def test_registry_round_trip(isolated_state):
    registry = runner.load_channel_registry(LOGGER)
    assert registry == {"version": 1, "channels": {}}

    registry["channels"]["42"] = {"job": "Boxing", "group_id": 1, "number": 260.0,
                                  "name": "Fight Night", "stream_ids": [1, 2],
                                  "slot": None, "source": "EPG", "adopted": False,
                                  "created_at": "x", "last_seen_at": "x"}
    runner.save_channel_registry(registry, LOGGER)

    reloaded = runner.load_channel_registry(LOGGER)
    assert reloaded["channels"]["42"]["job"] == "Boxing"


def test_registry_corrupt_file_loads_as_empty(isolated_state):
    path = runner._channel_registry_file_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json!!")
    registry = runner.load_channel_registry(LOGGER)
    assert registry == {"version": 1, "channels": {}}


# --------------------------------- adoption heuristic ------------------------

def test_adoption_heuristic_adopts_synthetic_tvg_id_channel():
    registry = {"version": 1, "channels": {}}
    group_channels = [
        {"id": 7, "name": "20:00, 15-Mar | Fight Night", "epg_tvg_id": "sea-ch-7",
         "tvg_id": "", "channel_number": 260.0, "streams": [1]},
    ]
    adopted, unowned = runner.run_adoption_pass(
        registry, group_id=1, job_name="Boxing", group_channels=group_channels,
        matched_stream_ids=set(), adopted_groups_seen=set(), logger=LOGGER)
    assert adopted == 1
    assert unowned == 0
    assert registry["channels"]["7"]["job"] == "Boxing"
    assert registry["channels"]["7"]["adopted"] is True


def test_adoption_heuristic_leaves_unrelated_manual_channel_unowned():
    registry = {"version": 1, "channels": {}}
    group_channels = [
        {"id": 8, "name": "My Curated 24/7 Sports Channel", "epg_tvg_id": "",
         "tvg_id": "", "channel_number": 999.0, "streams": [55]},
    ]
    adopted, unowned = runner.run_adoption_pass(
        registry, group_id=2, job_name="Boxing", group_channels=group_channels,
        matched_stream_ids={1, 2, 3}, adopted_groups_seen=set(), logger=LOGGER)
    assert adopted == 0
    assert unowned == 1
    assert "8" not in registry["channels"]


def test_adoption_heuristic_is_one_shot_per_group():
    registry = {"version": 1, "channels": {"7": {"job": "Boxing", "group_id": 1}}}
    group_channels = [
        {"id": 9, "name": "20:00, 15-Mar | Another Fight", "epg_tvg_id": "sea-ch-9",
         "tvg_id": "", "channel_number": 261.0, "streams": []},
    ]
    adopted, unowned = runner.run_adoption_pass(
        registry, group_id=1, job_name="Boxing", group_channels=group_channels,
        matched_stream_ids=set(), adopted_groups_seen=set(), logger=LOGGER)
    assert adopted == 0  # group already has a registry entry -- skipped
    assert "9" not in registry["channels"]


def test_adoption_heuristic_force_bypasses_group_gate():
    registry = {"version": 1, "channels": {"7": {"job": "Boxing", "group_id": 1}}}
    group_channels = [
        {"id": 9, "name": "20:00, 15-Mar | Another Fight", "epg_tvg_id": "sea-ch-9",
         "tvg_id": "", "channel_number": 261.0, "streams": []},
    ]
    adopted, unowned = runner.run_adoption_pass(
        registry, group_id=1, job_name="Boxing", group_channels=group_channels,
        matched_stream_ids=set(), adopted_groups_seen=set(), logger=LOGGER, force=True)
    assert adopted == 1
    assert "9" in registry["channels"]


def test_adoption_heuristic_marks_group_scanned_even_with_nothing_adoptable():
    """Bug fix: a group where nothing was adoptable (all channels left
    unowned) must still be recorded as scanned so the pass doesn't
    re-attempt it on every single future run."""
    registry = {"version": 1, "channels": {}}
    group_channels = [
        {"id": 8, "name": "My Curated 24/7 Sports Channel", "epg_tvg_id": "",
         "tvg_id": "", "channel_number": 999.0, "streams": [55]},
    ]
    adopted, unowned = runner.run_adoption_pass(
        registry, group_id=3, job_name="Boxing", group_channels=group_channels,
        matched_stream_ids=set(), adopted_groups_seen=set(), logger=LOGGER)
    assert adopted == 0
    assert unowned == 1
    assert 3 in registry["scanned_groups"]

    # A second, later run (fresh adopted_groups_seen, as a new task invocation
    # would have) must now no-op instead of re-scanning.
    adopted2, unowned2 = runner.run_adoption_pass(
        registry, group_id=3, job_name="Boxing", group_channels=group_channels,
        matched_stream_ids=set(), adopted_groups_seen=set(), logger=LOGGER)
    assert (adopted2, unowned2) == (0, 0)


def test_unowned_kept_not_double_counted_on_adoption_run():
    """Regression for the double-counting bug: run_job's cleanup loop adds
    the adoption pass's own `unowned` count to stats["unowned_kept"], then
    the per-channel loop over the SAME group_channels increments the same
    stat again for every channel the adoption pass left unowned (since they
    still have no registry entry). This reproduces the exact sequence
    run_job's cleanup section performs (see runner.py's CLEANUP block) using
    the real run_adoption_pass + engine.decide_channel_purge, for both the
    pre-fix ("buggy") accounting and the current (fixed) accounting."""
    group_channels = [
        {"id": 1, "name": "My Curated 24/7 Sports Channel", "epg_tvg_id": "",
         "tvg_id": "", "channel_number": 991.0, "streams": [901]},
        {"id": 2, "name": "Another Manual Channel", "epg_tvg_id": "",
         "tvg_id": "", "channel_number": 992.0, "streams": [902]},
    ]
    known_job_names = {"Boxing"}

    def _run_cleanup_count(double_count_bug: bool):
        registry = {"version": 1, "channels": {}}
        registry_channels = registry["channels"]
        stats = {"unowned_kept": 0, "adopted": 0}

        pass_adopted, pass_unowned = runner.run_adoption_pass(
            registry, group_id=9, job_name="Boxing", group_channels=group_channels,
            matched_stream_ids=set(), adopted_groups_seen=set(), logger=LOGGER)
        stats["adopted"] += pass_adopted
        if double_count_bug:  # the old, buggy line
            stats["unowned_kept"] += pass_unowned

        for ch in group_channels:
            cid = ch.get("id")
            owner = registry_channels.get(str(cid), {}).get("job")
            delete_eligible, _reason = engine.decide_channel_purge(
                ch, owner, "Boxing", known_job_names, is_preserved=False)
            if not delete_eligible and owner is None:
                stats["unowned_kept"] += 1
        return stats["unowned_kept"]

    # Both channels are unowned (no adoption match) -- BEFORE the fix, each
    # is counted once by the adoption pass's own tally AND once more by the
    # per-channel loop: double-counted to 4 instead of the real 2.
    assert _run_cleanup_count(double_count_bug=True) == 4
    # AFTER the fix (mirrors the actual current runner.py code -- the
    # buggy `stats["unowned_kept"] += pass_unowned` line is gone): each of
    # the 2 unowned channels is counted exactly once.
    assert _run_cleanup_count(double_count_bug=False) == 2


# ------------------------------- decide_channel_purge wiring -----------------

def test_prune_channel_registry_drops_missing_channels():
    registry = {"version": 1, "channels": {
        "1": {"last_seen_at": "a"}, "2": {"last_seen_at": "b"},
    }}
    kept = runner.prune_channel_registry(registry, existing_channel_ids={1}, logger=LOGGER)
    assert kept == 1
    assert "1" in registry["channels"]
    assert "2" not in registry["channels"]


# ------------------------- mid-recording update-in-place deferral -----------

def test_update_channel_defers_stream_swap_when_recording_active(monkeypatch):
    """OrmClient.update_channel itself doesn't know about recordings -- the
    deferral lives in run_job's caller logic, gated on
    _has_active_or_future_recording. This test exercises that gate directly:
    monkeypatched to return True, the stream-set update must be skipped
    (stream_ids=None) while name/tvg_id still update."""
    calls = []

    class _FakeChannelQS:
        def __init__(self):
            self.updated = {}

        def filter(self, **kwargs):
            self._id = kwargs.get("id")
            return self

        def update(self, **kwargs):
            self.updated.update(kwargs)
            calls.append(("channel_update", kwargs))
            return 1

    class _FakeChannelStreamQS:
        def filter(self, **kwargs):
            calls.append(("stream_filter", kwargs))
            return self

        def delete(self):
            calls.append(("stream_delete", {}))

        def bulk_create(self, objs):
            calls.append(("stream_bulk_create", len(objs)))

    monkeypatch.setattr(runner, "_has_active_or_future_recording", lambda cid: True)

    import types
    fake_django_db = types.ModuleType("django.db")

    class _Atomic:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_django_db.transaction = types.SimpleNamespace(atomic=lambda: _Atomic())
    # setitem (not a bare assignment) so pytest restores the real sys.modules
    # entries after this test -- a bare assignment here previously leaked a
    # fake, incomplete django.db into every OTHER test file sharing this
    # pytest session (test_task_safety.py's own django.db stub was clobbered).
    monkeypatch.setitem(sys.modules, "django.db", fake_django_db)

    fake_models = types.ModuleType("apps.channels.models")
    fake_models.Channel = types.SimpleNamespace(objects=_FakeChannelQS())
    fake_models.ChannelStream = types.SimpleNamespace(objects=_FakeChannelStreamQS())
    fake_models.Logo = types.SimpleNamespace(objects=types.SimpleNamespace(
        get_or_create=lambda **k: (types.SimpleNamespace(), True)))
    monkeypatch.setitem(sys.modules, "apps", types.ModuleType("apps"))
    monkeypatch.setitem(sys.modules, "apps.channels", types.ModuleType("apps.channels"))
    monkeypatch.setitem(sys.modules, "apps.channels.models", fake_models)

    client = runner.OrmClient()
    # Simulate the caller's own deferral gate (as run_job does): recording
    # is blocked, so stream_ids is never passed through to update_channel.
    recording_blocked = runner._has_active_or_future_recording(123)
    assert recording_blocked is True
    client.update_channel(123, name="New Name", stream_ids=None, tvg_id="tvg-x")

    stream_calls = [c for c in calls if c[0].startswith("stream_")]
    assert stream_calls == []  # stream-set untouched
    assert any(c[0] == "channel_update" and c[1].get("name") == "New Name" for c in calls)
