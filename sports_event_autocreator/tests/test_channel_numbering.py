"""
Unit tests for engine.py's v1.3.0 channel-numbering/ownership primitives:
NumberAllocator, decide_channel_purge, decide_channel_action,
epg_link_is_trustworthy, probe_budget_plan.

engine.py has no Django/network dependencies; loaded directly by file path
via importlib (same pattern as test_engine.py) so this file stays runnable
with plain `python3 -m pytest` regardless of what else is installed.

Run:  python3 -m pytest sports_event_autocreator/tests/test_channel_numbering.py
"""

import importlib.util
import os
from datetime import datetime, timedelta

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine.py")
_spec = importlib.util.spec_from_file_location("sea_engine_numbering", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)


# ------------------------------- NumberAllocator ----------------------------

def test_next_free_skips_occupied_number():
    alloc = engine.NumberAllocator(used={260.0}, blocked_ranges=[])
    num, reason = alloc.next_free(260, None, "Track & Field")
    assert num == 261.0
    assert reason.startswith("skipped")


def test_next_free_skips_entire_blocked_range_of_other_job():
    alloc = engine.NumberAllocator(used=set(), blocked_ranges=[(262, 268, "Basketball")])
    num, reason = alloc.next_free(262, None, "Track & Field")
    assert num == 269.0
    assert reason.startswith("skipped")


def test_next_free_allows_number_inside_own_blocked_range():
    alloc = engine.NumberAllocator(used=set(), blocked_ranges=[(260, 269, "Track & Field")])
    num, reason = alloc.next_free(262, 269, "Track & Field")
    assert num == 262.0
    assert reason == "ok"


def test_release_then_reuse():
    alloc = engine.NumberAllocator(used={300.0}, blocked_ranges=[])
    num, _ = alloc.next_free(300, None, "Job")
    assert num == 301.0  # 300 occupied
    alloc.release(300)
    num2, _ = alloc.next_free(300, None, "Job")
    assert num2 == 300.0  # freed, reused


def test_next_free_returns_none_at_end_boundary():
    alloc = engine.NumberAllocator(used=set(), blocked_ranges=[])
    num, reason = alloc.next_free(268, 269, "Job")
    assert num == 268.0
    num2, reason2 = alloc.next_free(270, 269, "Job")
    assert num2 is None
    assert "exhausted" in reason2


def test_float_int_equivalence():
    alloc = engine.NumberAllocator(used={260}, blocked_ranges=[])
    num, reason = alloc.next_free(260.0, None, "Job")
    assert num == 261.0  # 260 (int) treated same as 260.0
    alloc2 = engine.NumberAllocator(used={260.0}, blocked_ranges=[])
    alloc2.release(260)  # release with an int must free the float-stored entry
    num2, _ = alloc2.next_free(260, None, "Job")
    assert num2 == 260.0


def test_production_regression_260_270_cluster():
    """The exact production scenario from the plan: Track & Field starts at
    260 with no end_number, Basketball Euroleague starts at 270 (implied
    range for Track & Field is therefore 260-269). 15 candidates must all
    land in 260-269 and the next one must be unnumbered -- never spill into
    270+."""
    alloc = engine.NumberAllocator(
        used=set(), blocked_ranges=[(270, None, "Basketball Euroleague")])
    cursor = 260
    assigned = []
    for _ in range(15):
        num, reason = alloc.next_free(cursor, 269, "Track & Field")
        if num is None:
            break
        assigned.append(num)
        alloc.reserve(num)
        cursor = num + 1
    assert assigned == [float(n) for n in range(260, 270)]  # exactly 260..269
    assert len(assigned) == 10  # only 10 slots available (260-269)

    # The 11th candidate (of the 15) must be unnumbered, not spill to 270+.
    num, reason = alloc.next_free(cursor, 269, "Track & Field")
    assert num is None
    assert "exhausted" in reason


# ----------------------------- decide_channel_purge -------------------------

def test_decide_channel_purge_preserved_wins():
    delete_eligible, reason = engine.decide_channel_purge(
        {"id": 1}, owner="Boxing", job_name="Boxing", known_job_names={"Boxing"},
        is_preserved=True)
    assert delete_eligible is False
    assert reason == "preserved"


def test_decide_channel_purge_unowned_protected():
    delete_eligible, reason = engine.decide_channel_purge(
        {"id": 1}, owner=None, job_name="Boxing", known_job_names={"Boxing"},
        is_preserved=False)
    assert delete_eligible is False
    assert "unowned" in reason


def test_decide_channel_purge_owned_by_self():
    delete_eligible, reason = engine.decide_channel_purge(
        {"id": 1}, owner="Boxing", job_name="Boxing", known_job_names={"Boxing"},
        is_preserved=False)
    assert delete_eligible is True
    assert "this job" in reason


def test_decide_channel_purge_owned_by_other_configured_job():
    delete_eligible, reason = engine.decide_channel_purge(
        {"id": 1}, owner="Tennis", job_name="Boxing", known_job_names={"Boxing", "Tennis"},
        is_preserved=False)
    assert delete_eligible is False
    assert "Tennis" in reason


def test_decide_channel_purge_owned_by_removed_job_adopts():
    delete_eligible, reason = engine.decide_channel_purge(
        {"id": 1}, owner="OldJob", job_name="Boxing", known_job_names={"Boxing"},
        is_preserved=False)
    assert delete_eligible is True
    assert "adopting" in reason
    assert "OldJob" in reason


# ----------------------------- decide_channel_action -------------------------

def _dt(hour, minute=0):
    return datetime(2030, 3, 15, hour, minute)


def test_decide_channel_action_exact_identity_updates():
    desired = {"title_norm": "canelo vs charlo", "event_iso": "2030-03-15T20:00:00",
              "event_dt": _dt(20), "stream_ids": [1], "slot": None}
    candidates = [{"channel_id": 5, "title_norm": "canelo vs charlo",
                  "event_iso": "2030-03-15T20:00:00", "event_dt": _dt(20),
                  "stream_ids": [1], "slot": None}]
    action, target, reason = engine.decide_channel_action(desired, candidates, 3.0)
    assert action == "update"
    assert target == 5


def test_decide_channel_action_shift_within_tolerance_nearest_wins():
    desired = {"title_norm": "canelo vs charlo", "event_iso": "2030-03-15T20:30:00",
              "event_dt": _dt(20, 30), "stream_ids": [1], "slot": None}
    candidates = [
        {"channel_id": 1, "title_norm": "canelo vs charlo", "event_iso": "2030-03-15T18:00:00",
         "event_dt": _dt(18), "stream_ids": [], "slot": None},
        {"channel_id": 2, "title_norm": "canelo vs charlo", "event_iso": "2030-03-15T21:00:00",
         "event_dt": _dt(21), "stream_ids": [], "slot": None},
    ]
    action, target, reason = engine.decide_channel_action(desired, candidates, 3.0)
    assert action == "update"
    assert target == 2  # nearest (30 min away vs 2.5h away)


def test_decide_channel_action_shift_outside_tolerance_creates():
    desired = {"title_norm": "canelo vs charlo", "event_iso": "2030-03-16T20:00:00",
              "event_dt": _dt(20) + timedelta(days=1), "stream_ids": [1], "slot": None}
    candidates = [{"channel_id": 5, "title_norm": "canelo vs charlo",
                  "event_iso": "2030-03-15T20:00:00", "event_dt": _dt(20),
                  "stream_ids": [1], "slot": None}]
    action, target, reason = engine.decide_channel_action(desired, candidates, 3.0)
    assert action == "create"
    assert target is None


def test_decide_channel_action_split_mode_disambiguation_no_double_claim():
    """3 candidates share the same title+time (split-mode); resolved by
    stream-set match, then shared id, then slot -- and the caller (which
    removes a claimed candidate before the next call) must never let two
    desired entries claim the same channel."""
    common = {"title_norm": "grand prix", "event_iso": "2030-03-15T20:00:00",
             "event_dt": _dt(20)}
    candidates = [
        dict(common, channel_id=1, stream_ids=[10], slot=0),
        dict(common, channel_id=2, stream_ids=[11], slot=1),
        dict(common, channel_id=3, stream_ids=[12], slot=2),
    ]

    # Desired #1: exact stream-set match -> candidate 2.
    desired1 = dict(common, stream_ids=[11], slot=None)
    action1, target1, _ = engine.decide_channel_action(desired1, candidates, 3.0)
    assert target1 == 2
    candidates = [c for c in candidates if c["channel_id"] != target1]

    # Desired #2: shares no exact set but overlaps a stream id with candidate 3.
    desired2 = dict(common, stream_ids=[12, 99], slot=None)
    action2, target2, _ = engine.decide_channel_action(desired2, candidates, 3.0)
    assert target2 == 3
    candidates = [c for c in candidates if c["channel_id"] != target2]

    # Desired #3: no stream overlap, but slot matches candidate 1's slot=0.
    desired3 = dict(common, stream_ids=[999], slot=0)
    action3, target3, _ = engine.decide_channel_action(desired3, candidates, 3.0)
    assert target3 == 1

    assert {target1, target2, target3} == {1, 2, 3}  # no double-claim


# ----------------------------- epg_link_is_trustworthy -----------------------

def test_epg_link_trustworthy_time_and_title_match():
    programmes = [{"start_time": _dt(20), "title": "Real Madrid vs Barcelona"}]
    ok, reason = engine.epg_link_is_trustworthy(
        programmes, "Real Madrid vs Barcelona", _dt(20, 10), 90)
    assert ok is True


def test_epg_link_untrustworthy_time_match_unrelated_title():
    """The case that matters most: a generic 24/7 channel's own programme
    happens to be near the event time but is a totally different show."""
    programmes = [{"start_time": _dt(20), "title": "Generic Sports Roundup"}]
    ok, reason = engine.epg_link_is_trustworthy(
        programmes, "Real Madrid vs Barcelona", _dt(20, 10), 90)
    assert ok is False


def test_epg_link_untrustworthy_title_match_outside_tolerance():
    programmes = [{"start_time": _dt(15), "title": "Real Madrid vs Barcelona"}]
    ok, reason = engine.epg_link_is_trustworthy(
        programmes, "Real Madrid vs Barcelona", _dt(20), 90)
    assert ok is False


def test_epg_link_untrustworthy_empty_programme_list():
    ok, reason = engine.epg_link_is_trustworthy([], "Real Madrid vs Barcelona", _dt(20), 90)
    assert ok is False
    assert "no programmes" in reason


# ------------------------------- probe_budget_plan ---------------------------

def test_probe_budget_plan_even_division():
    plan, surplus = engine.probe_budget_plan(80, ["A", "B", "C", "D", "E", "F", "G", "H"])
    assert surplus == 0
    assert all(p["reserve"] == 10 for p in plan.values())


def test_probe_budget_plan_remainder_becomes_surplus():
    plan, surplus = engine.probe_budget_plan(10, ["A", "B", "C"])
    assert surplus == 1
    assert all(p["reserve"] == 3 for p in plan.values())


def test_probe_budget_plan_empty_job_list():
    plan, surplus = engine.probe_budget_plan(40, [])
    assert plan == {}
    assert surplus == 40


def test_probe_budget_plan_total_zero():
    plan, surplus = engine.probe_budget_plan(0, ["A", "B"])
    assert surplus == 0
    assert all(p["reserve"] == 0 for p in plan.values())


def test_probe_budget_plan_single_job_no_surplus():
    plan, surplus = engine.probe_budget_plan(40, ["A"])
    assert plan["A"]["reserve"] == 40
    assert surplus == 0
