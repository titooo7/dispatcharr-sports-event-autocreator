"""
Regression tests for the two v1.3.0 numbering bugs fixed in this pass:

Bug 1: "Run one job" (tasks.py narrowing ``jobs`` to a single job by name
BEFORE spans were computed) let a single job's implied range come out
unbounded, spilling past a neighboring job's range. Fixed by always sourcing
job_spans/blocked_ranges from every ENABLED, currently-configured job (not
the job_name-narrowed list) -- this file exercises the underlying
runner.compute_job_number_spans() directly, simulating exactly what tasks.py
now does vs. what it used to do.

Bug 2: two enabled jobs tied on the same start_number (neither setting
end_number) produced mutually-unbounded spans, which could hang
NumberAllocator.next_free() forever. Fixed with (a) a hard iteration cap in
next_free() itself (belt) and (b) a deterministic tie-break in
compute_job_number_spans() plus a new pre-flight warning in
validate_job_number_ranges() (braces).

Run:  python3 -m pytest sports_event_autocreator/tests/test_numbering_span_bugs.py
      (from the repo root, using the venv set up for this task:
       python3 -m venv .venv && .venv/bin/pip install pytest requests celery
       .venv/bin/python -m pytest sports_event_autocreator/tests/ -q)
"""

import os
import sys
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sports_event_autocreator import runner  # noqa: E402
from sports_event_autocreator import engine  # noqa: E402


def _job(name, start_number=None, end_number=None, enabled=True, group=None,
         purge_group=False, preserve_below=None, preserve_above=None):
    return SimpleNamespace(
        name=name, start_number=start_number, end_number=end_number,
        enabled=enabled, group=group, purge_group=purge_group,
        preserve_below=preserve_below, preserve_above=preserve_above)


# --------------------------- Bug 1: "Run one job" spill ----------------------

def _full_config():
    """Track & Field (260), Basketball Euroleague (270), Boxing (280),
    Motorbikes (290) -- each unbounded/implied against the others, matching
    the verifier's reproduction scenario."""
    return [
        _job("Track & Field", start_number=260),
        _job("Basketball Euroleague", start_number=270),
        _job("Boxing", start_number=280),
        _job("Motorbikes", start_number=290),
    ]


def test_full_run_bounds_track_and_field_to_269():
    all_jobs = _full_config()
    spans = runner.compute_job_number_spans(all_jobs)
    assert spans["Track & Field"] == (260, 269)


def test_bug1_reproduced_when_spans_computed_from_narrowed_single_job_list():
    """This is exactly the pre-fix bug: narrow ``jobs`` down to one job BEFORE
    computing spans. With no other job in the list, the span comes out
    unbounded -- reproducing the reported spill."""
    all_jobs = _full_config()
    narrowed = [j for j in all_jobs if j.name.lower() == "track & field"]
    buggy_spans = runner.compute_job_number_spans(narrowed)
    assert buggy_spans["Track & Field"] == (260, None)  # unbounded: the bug


def test_bug1_fixed_when_spans_sourced_from_full_enabled_job_list():
    """This is exactly what tasks.py now does: job_name narrows ``jobs`` (the
    jobs that actually get created/purged/updated), but job_spans is always
    computed from the full enabled/configured list, so the single requested
    job still can't spill into a neighbor's range."""
    all_jobs = _full_config()
    narrowed = [j for j in all_jobs if j.name.lower() == "track & field"]

    # tasks.py sources spans from a separately fetched full list, never from
    # the (possibly narrowed) ``jobs`` -- mirrored here directly.
    fixed_spans = runner.compute_job_number_spans([j for j in all_jobs if j.enabled])
    assert fixed_spans["Track & Field"] == (260, 269)

    blocked_ranges = [(name, lo, hi) for name, (lo, hi) in fixed_spans.items()]
    alloc = engine.NumberAllocator(used=set(), blocked_ranges=[
        (lo, hi, name) for name, lo, hi in blocked_ranges])

    # Allocate 15 numbers for the single-job run -- with the fix, allocation
    # must never cross into Basketball Euroleague's (270+) range.
    cursor = 260
    end = fixed_spans["Track & Field"][1]  # 269, as a real run would pass
    allocated = []
    for _ in range(15):
        num, reason = alloc.next_free(cursor, end, "Track & Field")
        if num is None:
            break
        allocated.append(num)
        alloc.reserve(num)
        cursor = num + 1
    assert allocated == [260.0, 261.0, 262.0, 263.0, 264.0, 265.0, 266.0,
                          267.0, 268.0, 269.0]
    assert all(n < 270 for n in allocated)


# ------------------------- Bug 2: tied start_number hang ---------------------

def test_tied_start_numbers_produce_mutually_unbounded_spans_pre_fix_shape():
    """Sanity check on the raw scenario: two jobs, same start_number, neither
    setting end_number. Without any tie-break this would be (260, None) for
    BOTH -- the exact input that hangs a naive next_free() search."""
    jobs = [_job("Job A", start_number=260), _job("Job B", start_number=260)]
    # The fix breaks the tie deterministically by name, so this now differs
    # from the raw "both None" shape -- confirmed by the next test.
    spans = runner.compute_job_number_spans(jobs)
    highs = {hi for _lo, hi in spans.values()}
    assert highs != {None}, "tie-break must prevent BOTH jobs from being unbounded"


def test_tie_break_bounds_all_but_last_tied_job():
    jobs = [_job("Job A", start_number=260), _job("Job B", start_number=260),
            _job("Job C", start_number=260)]
    spans = runner.compute_job_number_spans(jobs)
    # Alphabetically-last ("Job C") stays unbounded (nothing higher exists);
    # the others are bounded to a single slot so no two are ever mutually
    # unbounded with each other.
    assert spans["Job A"] == (260, 260)
    assert spans["Job B"] == (260, 260)
    assert spans["Job C"] == (260, None)


def test_next_free_hangs_would_occur_without_cap_now_terminates_promptly():
    """Simulates the exact hang scenario the verifier reproduced: call
    next_free(260, None, ...) where the *other* job's blocked range is also
    (260, None) -- i.e. as if the tie-break hadn't run. The allocator itself
    must still never hang, regardless of what span data reaches it."""
    alloc = engine.NumberAllocator(used=set(), blocked_ranges=[(260, None, "Job B")])
    num, reason = alloc.next_free(260, None, "Job A")
    assert num is None
    assert "exhausted search bound" in reason


def test_next_free_terminates_promptly_via_real_tie_broken_spans():
    """End-to-end with the actual (fixed) compute_job_number_spans output:
    must resolve immediately -- no hang -- for BOTH jobs, even though the
    misconfiguration (still flagged by the pre-flight warning) means Job A's
    single tie-broken slot is itself inside Job B's still-open range, so
    Job A correctly gets "range exhausted" rather than hanging. Job B (the
    alphabetically-last, still-unbounded job) allocates normally."""
    jobs = [_job("Job A", start_number=260), _job("Job B", start_number=260)]
    spans = runner.compute_job_number_spans(jobs)
    blocked_ranges = [(lo, hi, name) for name, (lo, hi) in spans.items()]
    alloc = engine.NumberAllocator(used=set(), blocked_ranges=blocked_ranges)

    num_a, reason_a = alloc.next_free(260, spans["Job A"][1], "Job A")
    assert num_a is None  # Job A's own slot is inside Job B's open range
    assert "exhausted" in reason_a

    num_b, reason_b = alloc.next_free(260, spans["Job B"][1], "Job B")
    assert num_b == 261.0  # 260 is inside Job A's own (blocking) slot
    assert reason_b.startswith("skipped")


def test_validate_job_number_ranges_warns_on_shared_start_number():
    jobs = [_job("Job A", start_number=260), _job("Job B", start_number=260)]
    warnings = runner.validate_job_number_ranges(jobs)
    shared = [w for w in warnings if "share the same start number" in w]
    assert len(shared) == 1
    assert "Job A" in shared[0]
    assert "Job B" in shared[0]
    assert "260" in shared[0]


def test_validate_job_number_ranges_no_shared_start_warning_when_distinct():
    jobs = [_job("Job A", start_number=260), _job("Job B", start_number=270)]
    warnings = runner.validate_job_number_ranges(jobs)
    assert not [w for w in warnings if "share the same start number" in w]


def test_validate_job_number_ranges_ignores_disabled_job_in_shared_start_check():
    jobs = [_job("Job A", start_number=260),
            _job("Job B", start_number=260, enabled=False)]
    warnings = runner.validate_job_number_ranges(jobs)
    assert not [w for w in warnings if "share the same start number" in w]
