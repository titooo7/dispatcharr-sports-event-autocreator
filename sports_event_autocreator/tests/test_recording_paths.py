"""
Unit tests for runner.py's auto-DVR recording paths (ensure_recording,
load_auto_dvr_index, plugin_state_dir, _under_recordings_root) using a fake
Recording model -- no Django/Postgres needed.

runner.py's Django imports are all function-local (verified as this test
file's first check, below), so importing THIS file alone works fine without
Django installed as long as `requests` is available in the environment
(pip install requests, alongside pytest). Running the WHOLE tests/ directory
in one session (as the command below does) also needs `celery` installed,
even though this file doesn't use it itself -- test_task_safety.py imports
tasks.py, which imports `celery` at module level, and pytest's collection
of the whole directory fails if any one file can't be imported.

Run:  python3 -m pytest sports_event_autocreator/tests/test_recording_paths.py
      (from the repo root, using the venv set up for this task:
       python3 -m venv .venv && .venv/bin/pip install pytest requests celery
       .venv/bin/python -m pytest sports_event_autocreator/tests/ -q)
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# --- Step 1 (per the task brief): verify runner.py imports without Django ---
# This import itself IS that verification: if runner.py had a module-level
# Django import, this would raise ImportError/ModuleNotFoundError here (no
# Django installed in this test environment) and the whole file would error
# at collection time instead of reaching any test body.
from sports_event_autocreator import runner  # noqa: E402
from sports_event_autocreator import engine  # noqa: E402


# --------------------------------------------------------------------------
# Fake Recording model
# --------------------------------------------------------------------------

class UpdateFieldsPassed(AssertionError):
    """Raised (loudly) if anything ever calls .save(update_fields=...) on an
    EXISTING recording -- the exact regression this test suite exists to
    catch. See the ground-truth note in ensure_recording's docstring."""


class FakeRecording:
    _autoid = 1

    def __init__(self, channel_id, start_time, end_time, custom_properties, manager):
        self.id = FakeRecording._autoid
        FakeRecording._autoid += 1
        self.channel_id = channel_id
        self.start_time = start_time
        self.end_time = end_time
        self.custom_properties = custom_properties
        self._manager = manager
        self._save_calls = []

    def save(self, *args, **kwargs):
        self._save_calls.append(kwargs)
        if "update_fields" in kwargs:
            raise UpdateFieldsPassed(
                f"save(update_fields=...) called on Recording {self.id} -- "
                f"this is the orphaned-schedule trap: it must always be a "
                f"plain save() with no update_fields.")
        # Persist into the manager's store (already mutated the object
        # in place, this just keeps the "DB" and the object consistent).
        self._manager.store[self.id] = self


class FakeQuerySet:
    def __init__(self, records):
        self._records = list(records)

    def only(self, *fields):
        return self

    def get(self, pk):
        for r in self._records:
            if r.id == pk:
                return r
        raise LookupError(f"No fake Recording with pk={pk}")

    def filter(self, **kwargs):
        def _match(rec):
            for key, val in kwargs.items():
                if key.endswith("__lt"):
                    if not (getattr(rec, key[:-4]) < val):
                        return False
                elif key.endswith("__gt"):
                    if not (getattr(rec, key[:-4]) > val):
                        return False
                elif getattr(rec, key) != val:
                    return False
            return True
        return FakeQuerySet([r for r in self._records if _match(r)])

    def __iter__(self):
        return iter(self._records)


class FakeManager:
    def __init__(self):
        self.store = {}

    @property
    def objects(self):
        return self

    def only(self, *fields):
        return FakeQuerySet(self.store.values())

    def filter(self, **kwargs):
        return FakeQuerySet(self.store.values()).filter(**kwargs)

    def get(self, pk):
        if pk not in self.store:
            raise LookupError(f"No fake Recording with pk={pk}")
        return self.store[pk]

    def create(self, channel_id, start_time, end_time, custom_properties):
        rec = FakeRecording(channel_id, start_time, end_time,
                            dict(custom_properties), self)
        self.store[rec.id] = rec
        return rec


def _fake_recording_model_factory():
    """A fresh FakeManager standing in for the `Recording` class itself
    (ensure_recording calls `_recording_model()` and then `.objects....` on
    the result, so the manager plays the role of both)."""
    return FakeManager()


class _NullLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass


class _CapturingLogger:
    """Records every message passed to any level, for tests that need to
    assert on actual log content (Fix 4: the shift-live warning must always
    fire and must never assert a false outcome)."""
    def __init__(self):
        self.messages = []

    def _rec(self, level, msg):
        self.messages.append((level, msg))

    def info(self, msg, *a, **k): self._rec("info", msg)
    def warning(self, msg, *a, **k): self._rec("warning", msg)
    def error(self, msg, *a, **k): self._rec("error", msg)
    def debug(self, msg, *a, **k): self._rec("debug", msg)

    def all_text(self) -> str:
        return "\n".join(m for _, m in self.messages)


@pytest.fixture
def fake_recording(monkeypatch):
    manager = _fake_recording_model_factory()
    monkeypatch.setattr(runner, "_recording_model", lambda: manager)
    return manager


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    """Isolate the DVR tombstone/state file to a scratch dir per test."""
    monkeypatch.setenv("DISPATCHARR_PLUGINS_DIR", str(tmp_path))
    return tmp_path


def _start_utc_naive(hour=20, minute=0, day=15):
    # Far enough in the future that ensure_recording's internal
    # datetime.now(timezone.utc) is always BEFORE these events, regardless
    # of when this test suite actually runs -- needed because ensure_recording
    # does not accept an injectable "now" (see the RESCHEDULE vs SHIFT_LIVE
    # test below, which specifically needs a genuinely-pending row).
    return datetime(2030, 3, day, hour, minute)


LOGGER = _NullLogger()


# --------------------------- create / tombstone ----------------------------

def test_create_then_writes_tombstone_entry(fake_recording, isolated_state):
    result = runner.ensure_recording(
        1, _start_utc_naive(), 2.0, 5.0, 30.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona")
    assert result == "created"
    assert len(fake_recording.store) == 1

    state = runner._load_dvr_state(LOGGER)
    assert len(state["created"]) == 1


def test_user_deletion_is_not_resurrected(fake_recording, isolated_state):
    result = runner.ensure_recording(
        1, _start_utc_naive(), 2.0, 5.0, 30.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona")
    assert result == "created"

    # Simulate the user deleting the row (but the tombstone in state
    # persists, since state is separate from the fake "DB").
    fake_recording.store.clear()

    result2 = runner.ensure_recording(
        1, _start_utc_naive(), 2.0, 5.0, 30.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona")
    assert result2 == "tombstoned"
    assert len(fake_recording.store) == 0


# --------------------- THE most important test in this suite ---------------

def test_extend_never_passes_update_fields(fake_recording, isolated_state):
    # Create a recording with a short end...
    runner.ensure_recording(
        1, _start_utc_naive(), 1.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona")

    # ... then feed a later end for the SAME event: must extend, and the
    # fake's save() must never see update_fields (it raises loudly if so).
    result = runner.ensure_recording(
        1, _start_utc_naive(), 3.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona")
    assert result == "extended"

    rec = next(iter(fake_recording.store.values()))
    assert len(rec._save_calls) == 1
    assert "update_fields" not in rec._save_calls[0]


def test_reschedule_never_passes_update_fields(fake_recording, isolated_state):
    runner.ensure_recording(
        1, _start_utc_naive(hour=20, minute=0), 1.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona")

    # Same title, +25 minutes -> within the default tolerance -> reschedule.
    result = runner.ensure_recording(
        1, _start_utc_naive(hour=20, minute=25), 1.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona")
    assert result == "rescheduled"

    rec = next(iter(fake_recording.store.values()))
    for call_kwargs in rec._save_calls:
        assert "update_fields" not in call_kwargs


# ------------------------------ concurrency cap -----------------------------

def test_cap_counts_row_with_no_status_key_at_all(fake_recording, isolated_state):
    # Simulate the exact suspected Django NULL-handling gap: a pending row
    # whose custom_properties has NO "status" key at all (not even "").
    props_no_status = {
        "auto_dvr": True,
        "event_start": _start_utc_naive(hour=20).replace(tzinfo=timezone.utc).isoformat(),
        "event_key": "existing-blocker|whatever",
        "event_title_norm": "existing blocker",
        "event_key_scheme": 2,
        "original_end": (_start_utc_naive(hour=22).replace(tzinfo=timezone.utc)).isoformat(),
        "program": {"title": "Some Other Event"},
        # deliberately no "status" key
    }
    fake_recording.create(
        channel_id=99,
        start_time=_start_utc_naive(hour=20).replace(tzinfo=timezone.utc),
        end_time=_start_utc_naive(hour=22).replace(tzinfo=timezone.utc),
        custom_properties=props_no_status,
    )

    result = runner.ensure_recording(
        1, _start_utc_naive(hour=20, minute=30), 1.0, 0.0, 0.0,
        {"program": {"title": "A Totally Different Event"}}, LOGGER,
        identity_title="A Totally Different Event", max_simultaneous=1)
    assert result == "capped"


def test_cap_allows_when_under_limit(fake_recording, isolated_state):
    result = runner.ensure_recording(
        1, _start_utc_naive(hour=20), 1.0, 0.0, 0.0,
        {"program": {"title": "Event One"}}, LOGGER,
        identity_title="Event One", max_simultaneous=2)
    assert result == "created"


def test_cap_counts_manual_non_auto_dvr_recording(fake_recording, isolated_state):
    """Fix 1 (CRITICAL) regression test: a MANUAL (non-plugin) Recording --
    no "auto_dvr" tag at all -- overlapping the desired window must still
    count against max_simultaneous_recordings. Before the fix, the cap only
    summed over load_auto_dvr_index()'s plugin-owned-only snapshot, so this
    row was invisible and the cap count was 0 for it."""
    manual_props = {"program": {"title": "Manually scheduled DVR recording"}}
    fake_recording.create(
        channel_id=77,
        start_time=_start_utc_naive(hour=20).replace(tzinfo=timezone.utc),
        end_time=_start_utc_naive(hour=22).replace(tzinfo=timezone.utc),
        custom_properties=manual_props,
    )

    # Sanity check confirming the row really is invisible to the plugin-
    # owned index -- that part of the behavior is correct and unchanged;
    # only the CAP count must reach beyond it.
    index = runner.load_auto_dvr_index(fake_recording)
    assert len(index) == 0

    result = runner.ensure_recording(
        1, _start_utc_naive(hour=20, minute=30), 1.0, 0.0, 0.0,
        {"program": {"title": "A Totally Different Event"}}, LOGGER,
        identity_title="A Totally Different Event", max_simultaneous=1)
    assert result == "capped"


def test_count_overlapping_recordings_directly_before_and_after_evidence(fake_recording, isolated_state):
    """Direct before/after evidence for Fix 1, per the done-criteria: shows
    the manual row counts 0 via the old (index-only) approach and 1 via the
    new _count_overlapping_recordings() query."""
    manual_props = {"program": {"title": "Manual recording"}}
    fake_recording.create(
        channel_id=77,
        start_time=_start_utc_naive(hour=20).replace(tzinfo=timezone.utc),
        end_time=_start_utc_naive(hour=22).replace(tzinfo=timezone.utc),
        custom_properties=manual_props,
    )
    desired_start = _start_utc_naive(hour=20, minute=30).replace(tzinfo=timezone.utc)
    desired_end = _start_utc_naive(hour=21, minute=30).replace(tzinfo=timezone.utc)

    # BEFORE (the bug): counting only over the plugin-owned index.
    index = runner.load_auto_dvr_index(fake_recording)
    old_style_count = sum(
        1 for row in index
        if row["start"] < desired_end and row["end"] > desired_start
        and row["status"] not in runner.engine.TERMINAL_STATUSES
    )
    assert old_style_count == 0  # the bug: the manual row is invisible

    # AFTER (the fix): the dedicated any-origin query.
    new_count = runner._count_overlapping_recordings(fake_recording, desired_start, desired_end)
    assert new_count == 1  # fixed: the manual row is counted


# ------------------------ no-event_start same-channel fallback (Fix 2) ------------------------

def test_no_parseable_event_start_row_tracked_as_unknown_channel(fake_recording, isolated_state):
    """Direct before/after evidence for Fix 2: a plugin-owned row we can't
    parse an event_start out of used to just vanish from the index (dropped
    silently); it must now show up in the index's unknown_channel_ids."""
    props_no_event_start = {
        "auto_dvr": True,
        "event_start": "",  # unparseable/missing
        "program": {"title": "Some Older Event"},
    }
    fake_recording.create(
        channel_id=5,
        start_time=_start_utc_naive(hour=18).replace(tzinfo=timezone.utc),
        end_time=_start_utc_naive(hour=20).replace(tzinfo=timezone.utc),
        custom_properties=props_no_event_start,
    )

    index = runner.load_auto_dvr_index(fake_recording)
    assert len(index) == 0  # BEFORE-equivalent: can't be reconciled as a full entry
    assert 5 in index.unknown_channel_ids  # AFTER: tracked as a same-channel blocker


def test_no_parseable_event_start_row_suppresses_duplicate_on_same_channel(fake_recording, isolated_state):
    """Fix 2 regression test, the verifier's exact repro: a plugin-owned row
    on the SAME channel with no parseable event_start must still suppress a
    new duplicate recording on that channel (pre-1.2.0 behavior)."""
    props_no_event_start = {
        "auto_dvr": True,
        "event_start": "",
        "program": {"title": "Some Older Event"},
    }
    fake_recording.create(
        channel_id=5,
        start_time=_start_utc_naive(hour=18).replace(tzinfo=timezone.utc),
        end_time=_start_utc_naive(hour=20).replace(tzinfo=timezone.utc),
        custom_properties=props_no_event_start,
    )

    result = runner.ensure_recording(
        5, _start_utc_naive(hour=20, minute=30), 1.0, 0.0, 0.0,
        {"program": {"title": "A New Different Event"}}, LOGGER,
        identity_title="A New Different Event")
    assert result == "skipped"
    assert len(fake_recording.store) == 1  # no duplicate created on that channel


def test_no_parseable_event_start_row_does_not_block_a_different_channel(fake_recording, isolated_state):
    """Sanity check: the unknown-channel fallback is scoped to its OWN
    channel only -- a different channel's event must still be created."""
    props_no_event_start = {
        "auto_dvr": True,
        "event_start": "",
        "program": {"title": "Some Older Event"},
    }
    fake_recording.create(
        channel_id=5,
        start_time=_start_utc_naive(hour=18).replace(tzinfo=timezone.utc),
        end_time=_start_utc_naive(hour=20).replace(tzinfo=timezone.utc),
        custom_properties=props_no_event_start,
    )

    result = runner.ensure_recording(
        6, _start_utc_naive(hour=20, minute=30), 1.0, 0.0, 0.0,
        {"program": {"title": "A New Different Event"}}, LOGGER,
        identity_title="A New Different Event")
    assert result == "created"
    assert len(fake_recording.store) == 2


# --------------- record_shift_tolerance_hours / record_max_extension_hours (Fix 3) ---------------

def test_ensure_recording_zero_shift_tolerance_creates_duplicate(fake_recording, isolated_state):
    runner.ensure_recording(
        1, _start_utc_naive(hour=20, minute=0), 1.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona")

    # Same title, +25 minutes -- would RESCHEDULE at the default tolerance,
    # but with shift_tolerance_hours explicitly 0 it must create a duplicate
    # instead (old exact-key-only behavior).
    result = runner.ensure_recording(
        1, _start_utc_naive(hour=20, minute=25), 1.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona", shift_tolerance_hours=0)
    assert result == "created"
    assert len(fake_recording.store) == 2


def test_ensure_recording_zero_max_extension_is_uncapped(fake_recording, isolated_state):
    runner.ensure_recording(
        1, _start_utc_naive(), 1.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona")

    # A MUCH later real end (10h) than the original (1h) -- with
    # max_extension_hours=0 the extension must not be capped at all.
    result = runner.ensure_recording(
        1, _start_utc_naive(), 10.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona", max_extension_hours=0)
    assert result == "extended"

    rec = next(iter(fake_recording.store.values()))
    expected_end = _start_utc_naive().replace(tzinfo=timezone.utc) + timedelta(hours=10.0)
    assert rec.end_time == expected_end  # uncapped: extended all the way


# ---------------------------------- shift-live (Fix 4) ---------------------------------------

def _make_live_recording(fake_recording, start_hour=20, duration_h=1.0, title="Real Madrid vs Barcelona"):
    """Directly create a plugin-owned Recording already in 'recording'
    status, bypassing ensure_recording (which never creates a row with
    status='recording' itself -- that's set by Dispatcharr's own capture
    once it actually starts)."""
    start = _start_utc_naive(hour=start_hour).replace(tzinfo=timezone.utc)
    end = start + timedelta(hours=duration_h)
    props = {
        "auto_dvr": True,
        "status": "recording",
        "event_start": start.isoformat(),
        "event_key": runner.engine.event_identity(title, start.isoformat()),
        "event_title_norm": runner.engine.normalize_event_title(title),
        "event_key_scheme": 2,
        "original_end": end.isoformat(),
        "program": {"title": title},
    }
    return fake_recording.create(channel_id=1, start_time=start, end_time=end, custom_properties=props)


def test_shift_live_warning_fires_even_when_shift_is_to_an_earlier_time(fake_recording, isolated_state):
    """Fix 4(a): the warning must fire unconditionally, even when the new
    EPG time is NOT later than the live recording's current end (here: a
    shift to an EARLIER time, so no extension happens at all)."""
    _make_live_recording(fake_recording, start_hour=20, duration_h=3.0)  # ends 23:00

    logger = _CapturingLogger()
    result = runner.ensure_recording(
        1, _start_utc_naive(hour=19, minute=45), 1.0, 0.0, 0.0,  # 15 min EARLIER
        {"program": {"title": "Real Madrid vs Barcelona"}}, logger,
        identity_title="Real Madrid vs Barcelona", max_simultaneous=5)

    assert "SHIFT-LIVE" in logger.all_text()
    assert not getattr(result, "also_extended", False)  # no extension happened
    assert result == "created"  # the second recording for the new time


def test_shift_live_capped_message_matches_actual_outcome(fake_recording, isolated_state):
    """Fix 4(b): when the cap blocks the second recording, the log must
    accurately say so -- never assert a second recording "will be created"
    when it wasn't."""
    _make_live_recording(fake_recording, start_hour=20, duration_h=1.0)  # ends 21:00

    logger = _CapturingLogger()
    result = runner.ensure_recording(
        1, _start_utc_naive(hour=20, minute=25), 3.0, 0.0, 0.0,  # shifted +25min, longer
        {"program": {"title": "Real Madrid vs Barcelona"}}, logger,
        identity_title="Real Madrid vs Barcelona", max_simultaneous=1)  # cap=1 -> blocks

    assert result == "capped"
    text = logger.all_text()
    assert "SHIFT-LIVE" in text
    assert "CAPPED" in text
    # Never claim a new recording WILL be / WAS created when it wasn't.
    assert "will also be created" not in text
    assert "second recording created" not in text


def test_shift_live_extension_and_second_recording_both_count_in_stats(fake_recording, isolated_state):
    """Fix 4(c): a shift-live event that BOTH extends the live recording AND
    creates a second recording must be reflected as +1 extended AND +1
    created, not just one of the two."""
    _make_live_recording(fake_recording, start_hour=20, duration_h=1.0)  # ends 21:00

    result = runner.ensure_recording(
        1, _start_utc_naive(hour=20, minute=25), 2.0, 0.0, 0.0,  # later end AND shifted
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona", max_simultaneous=5)

    assert result == "created"
    assert result.also_extended is True

    # Emulate exactly what both call sites do with the result.
    stats = {"created": 0, "extended": 0, "capped": 0, "rescheduled": 0}
    if result == "created":
        stats["created"] += 1
    elif result in ("extended", "rescheduled", "capped"):
        stats[result] += 1
    if getattr(result, "also_extended", False):
        stats["extended"] += 1

    assert stats["created"] == 1
    assert stats["extended"] == 1
    assert len(fake_recording.store) == 2  # original (extended) + new second recording


# --------------------------- in-memory index update -------------------------

def test_intra_run_dedup_via_shared_index(fake_recording, isolated_state):
    shared_index = runner.load_auto_dvr_index(fake_recording)

    result1 = runner.ensure_recording(
        1, _start_utc_naive(), 2.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona", index=shared_index)
    assert result1 == "created"

    # Second call for the SAME event, same run: must dedup via the shared
    # index (which was updated in place), not just against the fake "DB"
    # (which would also show it, but the point is the index path works).
    result2 = runner.ensure_recording(
        1, _start_utc_naive(), 2.0, 0.0, 0.0,
        {"program": {"title": "Real Madrid vs Barcelona"}}, LOGGER,
        identity_title="Real Madrid vs Barcelona", index=shared_index)
    assert result2 == "skipped"
    assert len(fake_recording.store) == 1


# ------------------------------ plugin_state_dir -----------------------------

def test_plugin_state_dir_migrates_legacy_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPATCHARR_PLUGINS_DIR", str(tmp_path))

    legacy_path = tmp_path / "legacy_auto_dvr_state.json"
    legacy_path.write_text('{"created": {"legacy-key|iso": {"end": "2026-01-01T00:00:00+00:00"}}}',
                           encoding="utf-8")

    new_path = os.path.join(runner.plugin_state_dir(), "auto_dvr_state.json")
    assert not os.path.exists(new_path)

    runner._migrate_legacy_state_file(new_path, str(legacy_path), LOGGER)

    assert os.path.exists(new_path)
    with open(new_path, encoding="utf-8") as f:
        import json
        data = json.load(f)
    assert "legacy-key|iso" in data["created"]


# --------------------------- _under_recordings_root --------------------------

@pytest.mark.parametrize("path,expected", [
    ("/data/recordings/x", True),
    ("/data/recordings", True),
    ("/data/logos", False),
    ("/data/recordings/../logos", False),
    ("/data/", False),
    ("", False),
    (None, False),
    (12345, False),
])
def test_under_recordings_root_table(path, expected):
    assert runner._under_recordings_root(path) is expected


# ---------- record_shift_tolerance_hours / record_max_extension_hours round-trip ----------
# (Fix 3, done-criteria #5: the two new per-job fields must not break
# normalize_job / job_from_settings / job_to_dict / settings_updates_from_jobs
# round-tripping -- this plugin's own settings-import path must not choke on
# them.)

def test_new_job_fields_have_defaults_in_normalize_job():
    job = runner.normalize_job({
        "name": "Test Job", "group": "Test Group", "search": ["boxing"],
    })
    assert job.record_shift_tolerance_hours == 3.0
    assert job.record_max_extension_hours == 2.0


def test_new_job_fields_round_trip_through_job_from_settings():
    settings = {
        runner.JOBS_LIST_KEY: "Test Job",
        runner.job_field_id("Test Job", "group"): "Test Group",
        runner.job_field_id("Test Job", "search"): "boxing",
        runner.job_field_id("Test Job", "record_shift_tolerance_hours"): 1.5,
        runner.job_field_id("Test Job", "record_max_extension_hours"): 0,
    }
    seeds = {}
    job = runner.job_from_settings(settings, "Test Job", seeds, active_epg_sources=[])
    assert job.record_shift_tolerance_hours == 1.5
    assert job.record_max_extension_hours == 0

    # job_to_dict / settings_updates_from_jobs must carry the values through
    # an export/import round-trip without choking on the new keys.
    as_dict = runner.job_to_dict(job)
    assert as_dict["record_shift_tolerance_hours"] == 1.5
    assert as_dict["record_max_extension_hours"] == 0

    updates = runner.settings_updates_from_jobs([job])
    assert updates[runner.job_field_id("Test Job", "record_shift_tolerance_hours")] == 1.5
    assert updates[runner.job_field_id("Test Job", "record_max_extension_hours")] == 0

    # Re-importing that JSON dict must not raise / reject the new keys.
    reimported = runner.normalize_job(as_dict)
    assert reimported.record_shift_tolerance_hours == 1.5
    assert reimported.record_max_extension_hours == 0


def test_negative_new_job_field_values_clamp_to_zero():
    settings = {
        runner.JOBS_LIST_KEY: "Test Job",
        runner.job_field_id("Test Job", "group"): "Test Group",
        runner.job_field_id("Test Job", "search"): "boxing",
        runner.job_field_id("Test Job", "record_shift_tolerance_hours"): -5,
        runner.job_field_id("Test Job", "record_max_extension_hours"): -1,
    }
    job = runner.job_from_settings(settings, "Test Job", {}, active_epg_sources=[])
    assert job.record_shift_tolerance_hours == 0.0
    assert job.record_max_extension_hours == 0.0
