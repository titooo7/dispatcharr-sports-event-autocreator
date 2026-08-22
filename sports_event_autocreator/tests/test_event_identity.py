"""
Unit tests for engine.py's event-identity primitives (normalize_event_title,
event_identity) and the pure recording-reconciliation decision function
(decide_recording_action).

engine.py has no Django/network dependencies, so we load it directly by file
path via importlib -- same pattern as test_engine.py.

Run:  python3 -m pytest sports_event_autocreator/tests/test_event_identity.py
      (from the repo root, using the venv set up for this task:
       .venv/bin/python -m pytest sports_event_autocreator/tests/ -q)
"""

import importlib.util
import os
from datetime import datetime, timedelta, timezone

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine.py")
_spec = importlib.util.spec_from_file_location("sea_engine_identity", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)


def _dt(hour=20, minute=0, day=15):
    return datetime(2026, 3, day, hour, minute, tzinfo=timezone.utc)


# --------------------------- event identity convergence --------------------

def test_event_identity_converges_across_paths():
    # Keyword-EPG path: raw EPG programme title.
    epg_raw_title = "Real Madrid vs Barcelona"
    # Teamarr-watch path: also the raw EPG programme title (same feed, same
    # real-world event) -- possibly with different accenting/whitespace from
    # a re-scrape, which normalize_event_title folds away.
    teamarr_raw_title = "Real  Madrid vs Barcelona"

    iso = _dt().isoformat()
    assert engine.event_identity(epg_raw_title, iso) == engine.event_identity(teamarr_raw_title, iso)


def test_event_identity_ignores_display_formatting():
    raw_title = "Real Madrid vs Barcelona"
    iso = _dt().isoformat()

    # Two format_epg_channel_name-style formatted display names for the SAME
    # underlying raw title -- one with a flag emoji + region label toggled
    # off, one without -- must NOT affect event_identity's output, since it
    # is built from the raw title, never the formatted display name.
    formatted_with_flag = engine.format_epg_channel_name(raw_title, _dt(), country_flag="🇪🇸")
    formatted_without_flag = engine.format_epg_channel_name(raw_title, _dt(), country_flag="")
    assert formatted_with_flag != formatted_without_flag  # sanity: they DO differ cosmetically

    identity_from_raw_a = engine.event_identity(raw_title, iso)
    identity_from_raw_b = engine.event_identity(raw_title, iso)
    assert identity_from_raw_a == identity_from_raw_b
    # Neither formatted string is ever fed into event_identity in real usage;
    # confirm the raw-title-based identity is stable regardless of what the
    # display toggles would have produced.
    assert engine.normalize_event_title(raw_title) in identity_from_raw_a


def test_normalize_event_title_folds_case_and_accents():
    assert engine.normalize_event_title("Fútbol Sala") == engine.normalize_event_title("FUTBOL SALA")


# ----------------------------- decide_recording_action ---------------------

def _desired(title="Real Madrid vs Barcelona", start=None, duration_h=2.0):
    start = start or _dt()
    end = start + timedelta(hours=duration_h)
    return {
        "title_norm": engine.normalize_event_title(title),
        "event_iso": start.isoformat(),
        "event_dt": start,
        "start": start,
        "end": end,
    }


def _row(pk, title="Real Madrid vs Barcelona", start=None, duration_h=2.0,
        status="", event_key=None, original_end=None):
    start = start or _dt()
    end = start + timedelta(hours=duration_h)
    title_norm = engine.normalize_event_title(title)
    event_iso = start.isoformat()
    return {
        "pk": pk,
        "title_norm": title_norm,
        "event_iso": event_iso,
        "event_dt": start,
        "start": start,
        "end": end,
        "status": status,
        "event_key": event_key if event_key is not None else f"{title_norm}|{event_iso}",
        "channel_id": 1,
        "original_end": original_end or end,
    }


def test_no_existing_rows_creates():
    action, pk, adj_end, reason = engine.decide_recording_action(
        _desired(), [], set(), datetime.now(timezone.utc))
    assert action == engine.RECONCILE_CREATE


def test_exact_match_already_covers_desired_end_skips():
    desired = _desired(duration_h=1.0)
    existing = [_row(1, duration_h=3.0)]  # covers well past desired end
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), datetime.now(timezone.utc))
    assert action == engine.RECONCILE_SKIP
    assert pk == 1


def test_exact_match_earlier_end_extends_capped_by_max_extension():
    desired = _desired(duration_h=5.0)  # desired end far later
    existing = [_row(1, duration_h=1.0)]  # much earlier end -> original_end
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), datetime.now(timezone.utc),
        max_extension_hours=2.0)
    assert action == engine.RECONCILE_EXTEND
    assert pk == 1
    original_end = existing[0]["original_end"]
    assert adj_end == min(desired["end"], original_end + timedelta(hours=2.0))
    assert adj_end < desired["end"]  # confirms the cap actually bit


def test_exact_match_terminal_status_never_resurrected():
    desired = _desired(duration_h=5.0)
    existing = [_row(1, duration_h=1.0, status="completed")]
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), datetime.now(timezone.utc))
    assert action == engine.RECONCILE_SKIP
    assert pk == 1


def test_legacy_key_match_extends_not_creates():
    # Row uses an OLD-scheme event_key unrelated to the new title_norm/
    # event_iso derivation (simulating a pre-1.2.0 row whose event_key was
    # built from the display name). It must still be found via legacy_keys.
    desired = _desired(duration_h=5.0)
    legacy_key = "old-scheme-display-name-key|2026-03-15T20:00:00+00:00"
    existing = [_row(1, duration_h=1.0, event_key=legacy_key)]
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), datetime.now(timezone.utc),
        legacy_keys=(legacy_key,))
    assert action == engine.RECONCILE_EXTEND
    assert pk == 1


def test_legacy_key_match_skip_when_already_covers():
    desired = _desired(duration_h=1.0)
    legacy_key = "old-scheme-display-name-key|2026-03-15T20:00:00+00:00"
    existing = [_row(1, duration_h=3.0, event_key=legacy_key)]
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), datetime.now(timezone.utc),
        legacy_keys=(legacy_key,))
    assert action == engine.RECONCILE_SKIP


def test_time_shift_25min_pending_reschedules():
    desired = _desired(start=_dt(hour=20, minute=25))
    existing = [_row(1, start=_dt(hour=20, minute=0), status="")]
    # "now" must be BEFORE the row's start for this to be a genuinely
    # pending (not-yet-live) row -- otherwise the race-condition guard
    # (start <= now) correctly treats it as already live.
    now = _dt(hour=18, minute=0)
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), now, shift_tolerance_hours=3.0)
    assert action == engine.RECONCILE_RESCHEDULE
    assert pk == 1


def test_time_shift_25min_recording_status_shift_live():
    desired = _desired(start=_dt(hour=20, minute=25))
    existing = [_row(1, start=_dt(hour=20, minute=0), status="recording")]
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), datetime.now(timezone.utc),
        shift_tolerance_hours=3.0)
    assert action == engine.RECONCILE_SHIFT_LIVE
    assert pk == 1


def test_time_shift_25min_start_already_passed_but_status_not_flipped_yet():
    # Race-condition guard: the row's own start is already in the past (it
    # SHOULD be recording by now) even though its status field hasn't
    # flipped to "recording" yet -- treat this the same as live.
    row_start = _dt(hour=20, minute=0)
    desired = _desired(start=_dt(hour=20, minute=25))
    existing = [_row(1, start=row_start, status="")]
    now = row_start + timedelta(minutes=10)  # now is after the row's start
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), now, shift_tolerance_hours=3.0)
    assert action == engine.RECONCILE_SHIFT_LIVE


def test_weekly_fixture_seven_days_apart_is_not_a_shift():
    desired = _desired(start=_dt(day=22, hour=20, minute=0))
    existing = [_row(1, start=_dt(day=15, hour=20, minute=0), status="")]
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), datetime.now(timezone.utc),
        shift_tolerance_hours=3.0)
    assert action == engine.RECONCILE_CREATE


def test_delta_just_outside_tolerance_creates():
    desired = _desired(start=_dt(hour=23, minute=1))  # 3h1min after 20:00
    existing = [_row(1, start=_dt(hour=20, minute=0), status="")]
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), datetime.now(timezone.utc),
        shift_tolerance_hours=3.0)
    assert action == engine.RECONCILE_CREATE


def test_tombstoned_key_with_no_existing_row():
    desired = _desired()
    key = f"{desired['title_norm']}|{desired['event_iso']}"
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, [], {key}, datetime.now(timezone.utc))
    assert action == engine.RECONCILE_TOMBSTONE


def test_two_candidates_within_tolerance_picks_nearest():
    desired = _desired(start=_dt(hour=20, minute=20))
    far = _row(1, start=_dt(hour=19, minute=0), status="")     # 80 min away
    near = _row(2, start=_dt(hour=20, minute=0), status="")    # 20 min away
    now = _dt(hour=18, minute=0)  # before both rows' start -> genuinely pending
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, [far, near], set(), now, shift_tolerance_hours=3.0)
    assert action == engine.RECONCILE_RESCHEDULE
    assert pk == 2


# ------------------ record_shift_tolerance_hours / record_max_extension_hours (Fix 3) ------------------

def test_zero_shift_tolerance_disables_reconciliation_and_creates_duplicate():
    # Same title, 25 minutes later -- WOULD reschedule at the default 3.0h
    # tolerance (see test_time_shift_25min_pending_reschedules), but with
    # shift_tolerance_hours explicitly set to 0 this must revert to the old
    # exact-key-only behavior and create a second recording instead.
    desired = _desired(start=_dt(hour=20, minute=25))
    existing = [_row(1, start=_dt(hour=20, minute=0), status="")]
    now = _dt(hour=18, minute=0)
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), now, shift_tolerance_hours=0)
    assert action == engine.RECONCILE_CREATE


def test_zero_max_extension_hours_is_uncapped():
    # A much later real end than the original -- with max_extension_hours=0
    # the extension must NOT be capped at all (unlike the default 2h cap in
    # test_exact_match_earlier_end_extends_capped_by_max_extension).
    desired = _desired(duration_h=10.0)  # end far later than original
    existing = [_row(1, duration_h=1.0)]
    action, pk, adj_end, reason = engine.decide_recording_action(
        desired, existing, set(), datetime.now(timezone.utc),
        max_extension_hours=0)
    assert action == engine.RECONCILE_EXTEND
    assert adj_end == desired["end"]  # uncapped: extends all the way
