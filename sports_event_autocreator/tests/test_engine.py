"""
Standalone unit tests for engine.py (the plugin's Django-free logic module).

engine.py has no Django/network dependencies, but the plugin package's other
modules (plugin.py, tasks.py, runner.py) do. To keep these tests runnable with
plain `python3 -m pytest`, we load engine.py directly by file path via importlib
instead of importing the package.

Run:  python3 -m pytest sports_event_autocreator/tests/test_engine.py
"""

import importlib.util
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine.py")
_spec = importlib.util.spec_from_file_location("sea_engine", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)


# --------------------------- parse_xmltv_time ---------------------------

def test_parse_xmltv_time_utc_offset():
    assert engine.parse_xmltv_time("20260315200000 +0000") == datetime(2026, 3, 15, 20, 0, 0)


def test_parse_xmltv_time_positive_offset():
    # 20:00 local at +0200 -> 18:00 UTC
    assert engine.parse_xmltv_time("20260315200000 +0200") == datetime(2026, 3, 15, 18, 0, 0)


def test_parse_xmltv_time_negative_offset_day_wrap():
    # 20:00 local at -0500 -> 01:00 UTC next day
    assert engine.parse_xmltv_time("20260315200000 -0500") == datetime(2026, 3, 16, 1, 0, 0)


def test_parse_xmltv_time_no_offset():
    assert engine.parse_xmltv_time("20260315200000") == datetime(2026, 3, 15, 20, 0, 0)


def test_parse_xmltv_time_garbage():
    assert engine.parse_xmltv_time("not a date") is None
    assert engine.parse_xmltv_time("") is None


# ------------------- extract_datetime_from_stream_name ------------------

def test_extract_dayname_day_month_time():
    dt, is_spec, wd = engine.extract_datetime_from_stream_name("Boxing Fri 20 Feb 23:00")
    assert is_spec and (dt.month, dt.day, dt.hour, dt.minute) == (2, 20, 23, 0)
    assert wd == "Fri"


def test_extract_day_month_24hour():
    dt, is_spec, _ = engine.extract_datetime_from_stream_name("Event 20 Feb 23:00")
    assert is_spec and (dt.month, dt.day, dt.hour, dt.minute) == (2, 20, 23, 0)


def test_extract_month_day_ampm():
    dt, is_spec, _ = engine.extract_datetime_from_stream_name("Feb 20 3:35 PM")
    assert is_spec and (dt.month, dt.day, dt.hour, dt.minute) == (2, 20, 15, 35)


def test_extract_parenthesized_month_day():
    dt, is_spec, _ = engine.extract_datetime_from_stream_name("Fight (Feb 12th 3:30pm)")
    assert is_spec and (dt.month, dt.day, dt.hour, dt.minute) == (2, 12, 15, 30)


def test_extract_explicit_year_not_nudged():
    # The explicit YYYY-MM-DD pattern keeps its literal year (no _nudge_year).
    dt, is_spec, _ = engine.extract_datetime_from_stream_name("Race (2026-03-15 20:00:00)")
    assert is_spec and (dt.year, dt.month, dt.day, dt.hour) == (2026, 3, 15, 20)


def test_extract_slash_day_month():
    dt, is_spec, _ = engine.extract_datetime_from_stream_name("Match 15/03 20:00")
    assert is_spec and (dt.month, dt.day, dt.hour, dt.minute) == (3, 15, 20, 0)


def test_extract_standalone_24hour():
    dt, is_spec, _ = engine.extract_datetime_from_stream_name("Boxeo 20:00")
    assert dt is not None and not is_spec and (dt.hour, dt.minute) == (20, 0)


def test_extract_standalone_ampm():
    dt, is_spec, _ = engine.extract_datetime_from_stream_name("Boxeo 8:00pm")
    assert dt is not None and dt.hour == 20


def test_extract_weekday_time():
    dt, is_spec, wd = engine.extract_datetime_from_stream_name("Rugby Sat 7:00am")
    assert dt is not None and (dt.hour, dt.minute) == (7, 0) and wd == "Sat"


def test_extract_no_datetime():
    dt, is_spec, wd = engine.extract_datetime_from_stream_name("Just A Plain Title")
    assert dt is None and not is_spec and wd is None


# ------------------------- _nudge_year (S2) -----------------------------

def test_nudge_year_far_future_shifts_back():
    far_future = datetime.now() + timedelta(days=300)
    nudged = engine._nudge_year(far_future)
    assert nudged.year == far_future.year - 1
    assert abs((nudged - datetime.now()).days) < 183


def test_nudge_year_far_past_shifts_forward():
    far_past = datetime.now() - timedelta(days=300)
    nudged = engine._nudge_year(far_past)
    assert nudged.year == far_past.year + 1
    assert abs((nudged - datetime.now()).days) < 183


def test_nudge_year_near_unchanged():
    near = datetime.now() + timedelta(days=10)
    assert engine._nudge_year(near) == near


# --------------------------- clean_stream_name --------------------------

def test_clean_stream_name_vs():
    out = engine.clean_stream_name("Boxing: Canelo vs Golovkin (Feb 12th)")
    assert "Canelo" in out and "vs" in out and "Golovkin" in out


def test_clean_stream_name_espn_prefix():
    out = engine.clean_stream_name("US (ESPN 5) | UFC Fight Night 20:00")
    assert "UFC Fight Night" in out
    assert "ESPN" not in out


# --------------------------- exclude_matches ----------------------------

def test_exclude_matches_word_boundary():
    assert engine.exclude_matches("UFC", "UFC Fight Night") is True
    # 'UFC' embedded inside a larger word must NOT match.
    assert engine.exclude_matches("UFC", "SUFCX Channel") is False


def test_exclude_matches_hashes_and_empty():
    assert engine.exclude_matches("##", "## test channel") is True
    assert engine.exclude_matches("", "anything") is False
    assert engine.exclude_matches("F1", "") is False


# --------------------------- detect_country_flag ------------------------

def test_detect_flag_prefix_beats_brand():
    # Leading 'TR:' (Turkey) must win over the beIN (Qatar) brand keyword.
    assert engine.detect_country_flag("TR: BEIN SPORTS 1") == "🇹🇷"


def test_detect_flag_brand_fallback():
    # No country-code prefix -> brand keyword resolves the flag.
    assert engine.detect_country_flag("beIN SPORTS HD") == "🇶🇦"


def test_detect_flag_unknown_code_computed():
    # Unknown 2-letter prefix -> computed regional-indicator flag.
    expected = "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in "XX")
    assert engine.detect_country_flag("XX: Mystery Channel") == expected


# ----------------- build_name_channel_name (B1 regression) --------------

def test_build_name_channel_confirmed_returns_naive_utc():
    stream = {"name": "ES: Boxeo Event 20:00", "channel_group": "", "m3u_account_name": ""}
    display_name, sort_dt, tz_conf, region = engine.build_name_channel_name(
        "ES: Boxeo Event 20:00", stream)

    assert tz_conf == engine.TZ_CONFIRMED
    # sort_dt must be NAIVE UTC: convert it back to Madrid and it round-trips
    # to the parsed local 20:00.
    assert sort_dt.tzinfo is None
    back_local = engine.convert_utc_to_display(sort_dt)
    assert back_local.strftime("%H:%M") == "20:00"

    # And it differs from the naive local 20:00 by the Madrid UTC offset.
    local_2000 = datetime.combine(date.today(), time(20, 0))
    madrid_offset = ZoneInfo("Europe/Madrid").utcoffset(local_2000)
    assert sort_dt == local_2000 - madrid_offset


# ------------- require_time date discriminator (v1.6.4 regression) -------

def test_bare_time_is_not_a_reliable_date():
    """runner._has_reliable_date skips names with a time but no date: the
    parser assumes 'today' for them, so old events would recreate daily."""
    for name in ("Rugby 5: France vs England 8:10pm",
                 "Team A vs Team B 20:00",
                 "Team A vs Team B"):
        dt, is_specific, weekday = engine.extract_datetime_from_stream_name(name)
        assert not (dt is not None and (is_specific or weekday is not None)), name


def test_dated_names_are_reliable():
    for name in ("Rugby: A vs B Sat 20 Feb 23:00",
                 "A vs B (2026-07-10 20:00:00)",
                 "A vs B Sat 7:00am"):
        dt, is_specific, weekday = engine.extract_datetime_from_stream_name(name)
        assert dt is not None and (is_specific or weekday is not None), name


# ------------------- parse_yavg_samples / classify_probe (v1.7.0) -------

# Realistic ffmpeg stderr: signalstats metadata frames interleaved with other
# log lines (input info, muxer stats). Only the YAVG values must be extracted.
_FFMPEG_STDERR = """\
Input #0, mpegts, from 'http://provider/stream':
  Duration: N/A, start: 1.400000, bitrate: N/A
  Stream #0:0[0x100]: Video: h264 (High), yuv420p, 1920x1080, 25 fps
Output #0, null, to 'pipe:':
frame=    1 pts=1400 pts_time=1.4
[Parsed_metadata_1 @ 0x55f0] lavfi.signalstats.YAVG=42.3
frame=    2 pts=1440 pts_time=1.44
[Parsed_metadata_1 @ 0x55f0] lavfi.signalstats.YAVG=43.1
[Parsed_metadata_1 @ 0x55f0] lavfi.signalstats.YAVG=41.8
frame=    3 pts=1480 pts_time=1.48
[out#0/null @ 0x55f0] video:12kB audio:0kB
"""


def test_parse_yavg_samples_extracts_all_values():
    samples = engine.parse_yavg_samples(_FFMPEG_STDERR)
    assert samples == [42.3, 43.1, 41.8]


def test_parse_yavg_samples_ignores_non_metadata_lines():
    stderr = ("frame= 1\nsome unrelated log line\n"
              "no YAVG here YAVG=oops\nDuration: N/A\n")
    assert engine.parse_yavg_samples(stderr) == []
    assert engine.parse_yavg_samples("") == []


def test_classify_probe_black():
    # Pure-black frames (~16 in YUV TV range) → below the 20 threshold.
    verdict, mean = engine.classify_probe([16.0, 16.2, 15.9])
    assert verdict == engine.PROBE_BLACK
    assert abs(mean - 16.03) < 0.1


def test_classify_probe_good():
    verdict, mean = engine.classify_probe([42.3, 43.1, 41.8])
    assert verdict == engine.PROBE_GOOD
    assert abs(mean - 42.4) < 0.1


def test_classify_probe_empty_is_indeterminate():
    verdict, mean = engine.classify_probe([])
    assert verdict == engine.PROBE_INDETERMINATE
    assert mean is None


def test_classify_probe_boundary_exactly_threshold_is_good():
    # Rule is strictly `< threshold` → mean of exactly 20.0 is GOOD.
    verdict, mean = engine.classify_probe([20.0, 20.0])
    assert verdict == engine.PROBE_GOOD
    assert mean == 20.0


# ---------------- pick_error_line (v1.7.2 regression) --------------------

def test_pick_error_line_prefers_root_cause_over_generic_tail():
    stderr = (
        "Input #0, mpegts, from 'http://x':\n"
        "[https @ 0x55] Server returned 403 Forbidden (access denied)\n"
        "Error opening output file -.\n"
        "Error opening output files: Invalid argument\n"
    )
    assert "403 Forbidden" in engine.pick_error_line(stderr)


def test_pick_error_line_falls_back_to_last_line_and_empty():
    assert engine.pick_error_line("just noise\nmore noise\n") == "more noise"
    assert engine.pick_error_line("") == ""
