"""
Pure matching/parsing/naming logic for the Sports Event Auto-Creator plugin.

Ported from 3-dispatcharr_hybrid_checker_fallback_ZAI_CLAUDE_v6.py.
This module has NO Django or network dependencies so it can be unit-tested
standalone. All Dispatcharr access lives in runner.py.

Two search methods are supported:
1. EPG-based: XMLTV programmes matched by title/description.
   Timezone always known from the XMLTV timestamp offset.
2. Name-based: Dispatcharr streams matched by name.
   Timezone inferred from region tokens in the stream/group/m3u name.

All display times are converted to the configured display timezone
(default Europe/Madrid).
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Display timezone (configurable via plugin settings)
# ---------------------------------------------------------------------------
_DISPLAY_TZ = ZoneInfo("Europe/Madrid")


def set_display_timezone(tz_name: str) -> None:
    global _DISPLAY_TZ
    try:
        _DISPLAY_TZ = ZoneInfo(tz_name)
    except Exception:
        _DISPLAY_TZ = ZoneInfo("Europe/Madrid")


# ---------------------------------------------------------------------------
# Region token → IANA timezone mapping (name-based streams only).
# Order matters: more specific patterns are listed first.
# ---------------------------------------------------------------------------
TIMEZONE_HINTS: List[Tuple[str, str, str]] = [
    # --- North America ---
    (r'\bUS[-\s]?ET\b',   "America/New_York",           "US Eastern"),
    (r'\bUS[-\s]?CT\b',   "America/Chicago",            "US Central"),
    (r'\bUS[-\s]?MT\b',   "America/Denver",             "US Mountain"),
    (r'\bUS[-\s]?PT\b',   "America/Los_Angeles",        "US Pacific"),
    # --- Special case: US| RUGBY PPV group is actually GMT ---
    (r'US\|.*?RUGBY.*?PPV', "Europe/London",            "GMT (US Rugby PPV)"),
    (r'\bUS\b',           "America/New_York",           "US"),
    (r'\bFLSP\b',         "America/New_York",           "US"),
    (r'\bCA\b',           "America/Toronto",            "Canada"),
    (r'\bMX\b',           "America/Mexico_City",        "México"),

    # --- South America ---
    (r'\bBR\b',           "America/Sao_Paulo",          "Brasil"),
    (r'\bAR\b',           "America/Argentina/Buenos_Aires", "Argentina"),
    (r'\bCL\b',           "America/Santiago",           "Chile"),
    (r'\bCO\b',           "America/Bogota",             "Colombia"),
    (r'\bPE\b',           "America/Lima",               "Perú"),

    # --- Europe ---
    (r'\bUK\b',           "Europe/London",              "UK"),
    (r'\bGB\b',           "Europe/London",              "UK"),
    (r'\bIE\b',           "Europe/Dublin",              "Ireland"),
    (r'\bES\b',           "Europe/Madrid",              "España"),
    (r'\bVO\b',           "Europe/Madrid",              "España"),
    (r'\bGO\b',           "Europe/Madrid",              "España"),
    (r'\bM\+',            "Europe/Madrid",              "España"),
    (r'\bPT\b',           "Europe/Lisbon",              "Portugal"),
    (r'\bFR\b',           "Europe/Paris",               "Francia"),
    (r'\bDE\b',           "Europe/Berlin",              "Alemania"),
    (r'\bIT\b',           "Europe/Rome",                "Italia"),
    (r'\bNL\b',           "Europe/Amsterdam",           "Países Bajos"),
    (r'\bBE\b',           "Europe/Brussels",            "Bélgica"),
    (r'\bPL\b',           "Europe/Warsaw",              "Polonia"),
    (r'\bRU\b',           "Europe/Moscow",              "Rusia"),
    (r'\bTR\b',           "Europe/Istanbul",            "Turquía"),
    (r'\bNO\b',           "Europe/Oslo",                "Noruega"),
    (r'\bSE\b',           "Europe/Stockholm",           "Suecia"),
    (r'\bDK\b',           "Europe/Copenhagen",          "Dinamarca"),
    (r'\bFI\b',           "Europe/Helsinki",            "Finlandia"),
    (r'\bCH\b',           "Europe/Zurich",              "Suiza"),
    (r'\bAT\b',           "Europe/Vienna",              "Austria"),
    (r'\bGR\b',           "Europe/Athens",              "Grecia"),
    (r'\bRO\b',           "Europe/Bucharest",           "Rumanía"),
    (r'\bCZ\b',           "Europe/Prague",              "República Checa"),
    (r'\bHU\b',           "Europe/Budapest",            "Hungría"),

    # --- Middle East & Africa ---
    (r'\bAE\b',           "Asia/Dubai",                 "EAU"),
    (r'\bSA\b',           "Asia/Riyadh",                "Arabia Saudí"),
    (r'\bZA\b',           "Africa/Johannesburg",        "Sudáfrica"),
    (r'\bEG\b',           "Africa/Cairo",               "Egipto"),
    (r'\bNG\b',           "Africa/Lagos",               "Nigeria"),

    # --- Asia & Oceania ---
    (r'\bIN\b',           "Asia/Kolkata",               "India"),
    (r'\bPK\b',           "Asia/Karachi",               "Pakistán"),
    (r'\bBD\b',           "Asia/Dhaka",                 "Bangladesh"),
    (r'\bSG\b',           "Asia/Singapore",             "Singapur"),
    (r'\bMY\b',           "Asia/Kuala_Lumpur",          "Malasia"),
    (r'\bID\b',           "Asia/Jakarta",               "Indonesia"),
    (r'\bPH\b',           "Asia/Manila",                "Filipinas"),
    (r'\bTH\b',           "Asia/Bangkok",               "Tailandia"),
    (r'\bVN\b',           "Asia/Ho_Chi_Minh",           "Vietnam"),
    (r'\bJP\b',           "Asia/Tokyo",                 "Japón"),
    (r'\bKR\b',           "Asia/Seoul",                 "Corea del Sur"),
    (r'\bCN\b',           "Asia/Shanghai",              "China"),
    (r'\bHK\b',           "Asia/Hong_Kong",             "Hong Kong"),
    (r'\bTW\b',           "Asia/Taipei",                "Taiwán"),
    (r'\bAU\b',           "Australia/Sydney",           "Australia"),
    (r'\bNZ\b',           "Pacific/Auckland",           "Nueva Zelanda"),

    # --- Provider/brand name hints (lower priority, listed last) ---
    (r'\bSTAN\b',         "Australia/Sydney",           "Australia (Stan)"),
    (r'\bTSN\b',          "America/Toronto",            "Canadá (TSN)"),
    (r'\bRTE\b',          "Europe/Dublin",              "Irlanda (RTÉ)"),
    (r'\bSKY\s*UK\b',     "Europe/London",              "UK (Sky)"),
    (r'\bBT\s*SPORT\b',   "Europe/London",              "UK (BT Sport)"),
    (r'\bNOW\b',          "Europe/London",              "UK (NOW TV)"),
    (r'\bDAZN\s*ES\b',    "Europe/Madrid",              "España (DAZN)"),
    (r'\bDAZN\s*IT\b',    "Europe/Rome",                "Italia (DAZN)"),
    (r'\bDAZN\s*DE\b',    "Europe/Berlin",              "Alemania (DAZN)"),
    (r'\bDAZN\s*FR\b',    "Europe/Paris",               "Francia (DAZN)"),
    (r'\bFLORUGBY\b',     "Europe/Madrid",              "España (FloRugby)"),
    (r'\bbeIN\s*SPORTS\b', "Asia/Qatar",                "Qatar (beIN)"),
    (r'\bSUPERSPORT\b',   "Africa/Johannesburg",        "Sudáfrica (SuperSport)"),
]

_COMPILED_TZ_HINTS = [
    (re.compile(pattern, re.IGNORECASE), tz, label)
    for pattern, tz, label in TIMEZONE_HINTS
]


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------

def infer_timezone_from_text(text: str) -> Optional[Tuple[str, str]]:
    """Scan text for known region tokens; return (IANA tz, label) or None."""
    for compiled_pattern, tz_name, label in _COMPILED_TZ_HINTS:
        if compiled_pattern.search(text):
            return tz_name, label
    return None


def apply_timezone_offset(naive_dt: datetime, tz_name: str) -> Optional[datetime]:
    """
    Given a naive datetime (assumed local in tz_name), return a naive UTC
    datetime, or None if the timezone name is invalid.
    """
    try:
        local_dt = naive_dt.replace(tzinfo=ZoneInfo(tz_name))
        return local_dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def convert_utc_to_display(utc_naive: datetime) -> datetime:
    """Convert a naive UTC datetime to display-timezone local time."""
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(_DISPLAY_TZ)


def convert_display_to_utc(local_naive: datetime) -> datetime:
    """Convert a naive display-timezone datetime to naive UTC."""
    try:
        local_dt = local_naive.replace(tzinfo=_DISPLAY_TZ)
        return local_dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return local_naive


# ---------------------------------------------------------------------------
# XMLTV parsing helpers (Phase 1 only)
# ---------------------------------------------------------------------------

def parse_xmltv_time(time_str: str) -> Optional[datetime]:
    """
    Parse an XMLTV datetime string (YYYYMMDDHHMMSS +TZON) into a naive UTC
    datetime. The explicit offset in the XMLTV data is used directly.
    """
    try:
        parts = time_str.split()
        if len(parts) < 2:
            try:
                return datetime.strptime(time_str[:14], "%Y%m%d%H%M%S")
            except ValueError:
                return None

        dt_part, tz_part = parts[0], parts[1]
        year    = int(dt_part[0:4])
        month   = int(dt_part[4:6])
        day     = int(dt_part[6:8])
        hour    = int(dt_part[8:10])
        minute  = int(dt_part[10:12])
        second  = int(dt_part[12:14]) if len(dt_part) >= 14 else 0

        tz_sign    = 1 if tz_part[0] == '+' else -1
        tz_hours   = int(tz_part[1:3])
        tz_minutes = int(tz_part[3:5])

        dt = datetime(year, month, day, hour, minute, second)
        total_offset = tz_sign * (tz_hours * 60 + tz_minutes)
        return dt - timedelta(minutes=total_offset)  # naive UTC
    except (ValueError, IndexError):
        try:
            return datetime.strptime(time_str[:14], "%Y%m%d%H%M%S")
        except Exception:
            return None


MONTHS_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
             "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


def format_epg_channel_name(title: str, start_time_utc: datetime, country_flag: str = "") -> str:
    """
    Format an EPG-based channel name. Time is converted from UTC to display
    local time. Compact format: HH:MM, DD-MMM (year omitted).
    """
    display_time = convert_utc_to_display(start_time_utc)
    time_str = f"{display_time.strftime('%H:%M')}, {display_time.day}-{MONTHS_ES[display_time.month - 1]}"
    prefix = f"{country_flag} " if country_flag else ""
    return f"{prefix}{time_str} | {title}"


# ---------------------------------------------------------------------------
# Name-based stream helpers (Phase 2 only)
# ---------------------------------------------------------------------------

def _get_next_weekday(weekday_str: str, from_dt: datetime = None) -> datetime:
    """Calculate the next occurrence of a weekday from from_dt (inclusive)."""
    if from_dt is None:
        from_dt = datetime.now()
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    target = days.index(weekday_str[:3].lower())
    current = from_dt.weekday()
    days_ahead = target - current
    if days_ahead < 0:
        days_ahead += 7
    return from_dt + timedelta(days=days_ahead)


_MONTHS_MAP = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
               "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _nudge_year(dt: datetime) -> datetime:
    """
    Correct the year for month/day-only parses that assumed the current year.
    A late-December event parsed in January (and vice versa) lands ~11 months
    off; if the result is more than ~6 months away from now, shift the year so
    it falls in the nearest wrap. Only for parsers with no explicit year.
    """
    now = datetime.now()
    if dt - now > timedelta(days=183):
        return dt.replace(year=dt.year - 1)
    if now - dt > timedelta(days=183):
        return dt.replace(year=dt.year + 1)
    return dt


def _apply_ampm(hour: int, ampm: Optional[str]) -> int:
    if ampm:
        ampm = ampm.lower()
        if ampm == 'pm' and hour != 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0
    return hour


def _parse_dayname_day_month_time(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse dayname + day + month + time like 'Fri 20 Feb 23:00' or 'Sat 3 Feb 3:30pm'."""
    try:
        weekday_str = m.group(0).split()[0]
        day = int(m.group(1))
        month_str = m.group(2)
        hour = _apply_ampm(int(m.group(3)), m.group(5))
        minute = int(m.group(4))
        month = _MONTHS_MAP.get(month_str[:3].lower())
        if not month:
            return None, False, None
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return _nudge_year(datetime(datetime.now().year, month, day, hour, minute)), True, weekday_str[:3].title()
    except (ValueError, AttributeError):
        pass
    return None, False, None


def _parse_month_day_ampm(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse month + day + 12-hour time like 'Feb 20 3:35 PM'."""
    try:
        month_str = m.group(1)
        day = int(m.group(2))
        hour = _apply_ampm(int(m.group(3)), m.group(5))
        minute = int(m.group(4))
        month = _MONTHS_MAP.get(month_str[:3].lower())
        if not month:
            return None, False, None
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return _nudge_year(datetime(datetime.now().year, month, day, hour, minute)), True, None
    except (ValueError, AttributeError):
        pass
    return None, False, None


def _parse_day_month_24hour(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse day + month + 24-hour time like '20 Feb 23:00'."""
    try:
        day = int(m.group(1))
        month_str = m.group(2)
        hour = int(m.group(3))
        minute = int(m.group(4))
        month = _MONTHS_MAP.get(month_str[:3].lower())
        if month and 0 <= hour <= 23 and 0 <= minute <= 59:
            return _nudge_year(datetime(datetime.now().year, month, day, hour, minute)), True, None
    except (ValueError, AttributeError):
        pass
    return None, False, None


def _parse_month_day_24hour(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse month + day + 24-hour time like 'Feb 20 23:00'."""
    try:
        month_str = m.group(1)
        day = int(m.group(2))
        hour = int(m.group(3))
        minute = int(m.group(4))
        month = _MONTHS_MAP.get(month_str[:3].lower())
        if month and 0 <= hour <= 23 and 0 <= minute <= 59:
            return _nudge_year(datetime(datetime.now().year, month, day, hour, minute)), True, None
    except (ValueError, AttributeError):
        pass
    return None, False, None


def _parse_time_weekday(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse time + weekday like '7:00am Sat' or '23:00 Friday'."""
    try:
        hour = _apply_ampm(int(m.group(1)), m.group(3))
        minute = int(m.group(2))
        weekday_str = m.group(4)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            dt = _get_next_weekday(weekday_str)
            return dt.replace(hour=hour, minute=minute, second=0, microsecond=0), False, weekday_str[:3].title()
    except (ValueError, AttributeError):
        pass
    return None, False, None


def _parse_weekday_time(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse weekday + time like 'Sat 7:00am'."""
    try:
        weekday_str = m.group(1)
        hour = _apply_ampm(int(m.group(2)), m.group(4))
        minute = int(m.group(3))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            dt = _get_next_weekday(weekday_str)
            return dt.replace(hour=hour, minute=minute, second=0, microsecond=0), False, weekday_str[:3].title()
    except (ValueError, AttributeError):
        pass
    return None, False, None


def _parse_parenthesized_month_day(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse parenthesized month/day like '(feb 3rd)' or '(Feb 12th 3:30pm)'."""
    try:
        month_str = m.group(1)
        day = int(m.group(2))
        month = _MONTHS_MAP.get(month_str[:3].lower())
        if not month:
            return None, False, None
        if m.group(3) is not None and m.group(4) is not None:
            hour = _apply_ampm(int(m.group(3)), m.group(5))
            minute = int(m.group(4))
            if hour <= 23 and minute <= 59:
                return _nudge_year(datetime(datetime.now().year, month, day, hour, minute)), True, None
        return _nudge_year(datetime(datetime.now().year, month, day)), True, None
    except (ValueError, AttributeError):
        pass
    return None, False, None


def _parse_day_month(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    try:
        day, month, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        if month > 12:
            day, month = month, day
        return _nudge_year(datetime(datetime.now().year, month, day, hour, minute)), True, None
    except ValueError:
        return None, False, None


def _parse_standalone_time_ampm(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse standalone 12-hour time like '8:00pm' from anywhere in string."""
    try:
        hour = _apply_ampm(int(m.group(1)), m.group(3))
        minute = int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            now = datetime.now()
            return datetime(now.year, now.month, now.day, hour, minute), False, None
    except (ValueError, AttributeError):
        pass
    return None, False, None


def _parse_standalone_time_24hour(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse standalone 24-hour time like '20:00' from anywhere in string."""
    try:
        hour = int(m.group(1))
        minute = int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            now = datetime.now()
            return datetime(now.year, now.month, now.day, hour, minute), False, None
    except (ValueError, AttributeError):
        pass
    return None, False, None


def _parse_standalone_hour_ampm(m) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """Parse a standalone hour with am/pm and no minutes, e.g. '10am', '1pm'."""
    try:
        hour = _apply_ampm(int(m.group(1)), m.group(2))
        if 0 <= hour <= 23:
            now = datetime.now()
            return datetime(now.year, now.month, now.day, hour, 0), False, None
    except (ValueError, AttributeError):
        pass
    return None, False, None


# Patterns that identify date/time info embedded in stream names.
# Order matters: more specific patterns are listed first.
_DATE_PATTERNS = [
    (r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+(\d{1,2})\s+([A-Za-z]{3})\s+(\d{1,2}):(\d{2})\s*([ap]m)?\b',
     _parse_dayname_day_month_time),
    (r'\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{1,2}):(\d{2})\b',
     _parse_day_month_24hour),
    (r'\b([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})\s*([ap]m)\b',
     _parse_month_day_ampm),
    (r'\b([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})\b',
     _parse_month_day_24hour),
    (r'\(([A-Za-z]{3})\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{1,2}):(\d{2})(?:\s*([ap]m))?)?\)',
     _parse_parenthesized_month_day),
    (r'\((\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\)',
     lambda m: (datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         int(m.group(4)), int(m.group(5)), int(m.group(6))), True, None)),
    (r'\b(\d{1,2}):(\d{2})\s*([ap]m)?\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b',
     _parse_time_weekday),
    (r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+(\d{1,2}):(\d{2})\s*([ap]m)?\b',
     _parse_weekday_time),
    (r'\b(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\b',
     _parse_day_month),
    (r'\b(\d{1,2}):(\d{2})\s*([ap]m)\b',
     _parse_standalone_time_ampm),
    (r'\b(\d{1,2}):(\d{2})\b',
     _parse_standalone_time_24hour),
    (r'\b(\d{1,2})\s*([ap]m)\b',
     _parse_standalone_hour_ampm),
]


def extract_datetime_from_stream_name(stream_name: str) -> Tuple[Optional[datetime], bool, Optional[str]]:
    """
    Try each date pattern in order.
    Returns: (parsed_datetime, is_specific_date, found_weekday_str)
    """
    for pattern, parser in _DATE_PATTERNS:
        m = re.search(pattern, stream_name, re.IGNORECASE)
        if m:
            try:
                dt, is_spec, wd = parser(m)
                if dt:
                    return dt, is_spec, wd
            except (ValueError, AttributeError):
                continue
    return None, False, None


# Patterns to strip from stream names to produce a clean channel name.
_CLEANUP_PATTERNS = [
    r'\(FLSP\s+\d+\)\s*\|\s*florugby:\s*',
    r'\(MX\)\s*\(Disney\s+\d+\)\s*\|\s*',
    r'^AU\s*\(STAN\s+\d+\)\s*\|\s*',
    r'^CBC\s*\d+:\s*sports\|',
    r'\s*\(\d{4}-\d{2}-\d{2}[^)]*\)',
    r'\s*@\s*[A-Za-z]{3}\s+\d+\s+[\d:]+\s*[AP]M*',
    r'\s*:\s*TSN\+.*$',
    r'\s*\(Round\s+\d+\).*$',
    r'_\s*.*$',
    r'\s*\d{2}:\d{2}:\d{2}\s*$',
    r'\s*\d{1,2}:\d{2}[ap]m\s*$',
    r'\s*\d{1,2}:\d{2}\s*[AP]M\s*$',
    r'\s*(Sun|Sat|Mon|Tue|Wed|Thu|Fri)[a-z]*\s*$',
    r'\s*\|\s*(Sun|Sat|Mon|Tue|Wed|Thu|Fri)[a-z]*\s*$',
    r'\s*\|\s*\dK\s+(?:UHD|EXCLUSIVE|HDR|HDR10\+?)\b',
    r'\s*\|\s*\dK\s*$',
    r'\s*\|\s*(?:UHD|4K|8K)\b',
    r'\s*-\s*(?:UHD|4K|8K)\s*EXCLUSIVE\b',
    r'\b[A-Z]{2}:\s*\S+\s+PPV\s+\d+\s*-\s*',
    r'\s*\(\s*(?:Day\s+(?:One|Two|Three|Four|Five|Six|Seven|1|2|3|4|5|6|7)|\d{1,2}(?:st|nd|rd|th))\s*\)',
    r'\s*\|\s*[A-Z][a-z]{2,3}\s+\d{1,2}\s+[\d:]+(?:\s*[AP]M?)?\b',
    r'\s*\|\s*[A-Z][a-z]{2,3}\s+\d{1,2}\s+\d{4}\b',
    r'\s*\|\s*DK:\s*\S+',
    r'\s*\|\s*ESPN\s+\d+\s*\|',
    r'\s*\(.*?\bvs\.?\b.*?\)\s*$',
    r'[ᴿᴬᵂᴸᶦᵛᴱ₋₀-₉]',
    r'^\s*\([A-Z]{2,3}\)\s*\|\s*',
    r'^\s*\([A-Z]{2,3}\)\s+',
    r'^[A-Z]{2}:\s*',
    r'^US\s*\(ESPN\+\s*\d+\)\s*\|\s*',  # ESPN+ with plus - must come before ESPN pattern
    r'^US\s*\(ESPN\s+\d+\)\s*\|\s*',    # ESPN without plus
    r'\s*-\s*\d{1,2}:\d{2}\s*$',
]
_COMPILED_CLEANUP = [re.compile(p, re.IGNORECASE) for p in _CLEANUP_PATTERNS]


def _is_noise_segment(segment: str) -> bool:
    segment = segment.strip()
    if not segment:
        return True
    if re.match(r'^[A-Z][a-z]{2,3}\s+\d{1,2}\s+[\d:]+(?:\s*[AP]M?)?$', segment):
        return True
    if re.match(r'^[A-Z][a-z]{2,3}\s+\d{1,2}\s+\d{4}$', segment):
        return True
    if re.match(r'^\dK\s+(?:UHD|EXCLUSIVE|HDR|HDR10\+?)\b', segment, re.IGNORECASE):
        return True
    if re.match(r'^(?:UHD|4K|8K)\b', segment, re.IGNORECASE):
        return True
    if re.match(r'^[A-Z]{2}:\s*\S+', segment):
        return True
    if re.search(r'\bPPV\s+\d+\s*$', segment):
        return True
    if re.match(r'^ESPN\s+\d+\s*$', segment):
        return True
    if re.match(r'^(?:Sun|Sat|Mon|Tue|Wed|Thu|Fri)[a-z]*\s*(?:\d{1,2}:\d{2}\s*(?:[ap]m)?)?$', segment, re.IGNORECASE):
        return True
    return False


def extract_meaningful_title(raw_name: str) -> str:
    name = re.sub(r'^[A-Z]{2}:\s*\S+(?:\s+PPV\s+\d+)?\s*-\s*', '', raw_name)
    name = re.sub(r'^US\s*\(ESPN\+\s*\d+\)\s*\|\s*', '', name)
    name = re.sub(r'^US\s*\(ESPN\s+\d+\)\s*\|\s*', '', name)
    name = re.sub(r'^AU\s*\(STAN\s+\d+\)\s*\|\s*', '', name)
    name = re.sub(r'^\(FLSP\s+\d+\)\s*\|\s*flo\w+:\s*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[ᴿᴬᵂᴸᶦᵛᴱ₋₀-₉]', '', name)
    name = re.sub(r'\|\s*\dK\s+(?:UHD|EXCLUSIVE|HDR|HDR10\+?)\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\|\s*(?:UHD|4K|8K)\b', '', name, flags=re.IGNORECASE)

    vs_patterns = [
        r'([A-Z][A-Za-z\s&\'\-]+)\s+v\s+([A-Z][A-Za-z\s&\'\-]+)',
        r'([A-Z][A-Za-z\s&\'\-]+)\s+vs\.?\s+([A-Z][A-Za-z\s&\'\-]+)',
        r'([A-Z][A-Za-z\s&\'\-]+)\s+VERSUS\s+([A-Z][A-Za-z\s&\'\-]+)',
    ]
    for pattern in vs_patterns:
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            team_a = ' '.join(match.group(1).strip().split())
            team_b = ' '.join(match.group(2).strip().split())
            return f"{team_a} vs {team_b}"

    if '|' in name:
        parts = [p.strip() for p in name.split('|')]
        if len(parts) > 1:
            meaningful_parts = [p for p in parts if not _is_noise_segment(p)]
            if meaningful_parts:
                return meaningful_parts[0]
            return parts[-1]

    name = re.sub(r'\s*\(([^)]*\bvs\.?\b[^)]*)\)\s*$', r' \1', name, flags=re.IGNORECASE)
    name = re.sub(r'\|\s*[A-Z][a-z]{2,3}\s+\d{1,2}\s+[\d:]+(?:\s*[AP]M?)?\b', '', name)
    name = re.sub(r'\|\s*[A-Z][a-z]{2,3}\s+\d{1,2}\s+\d{4}\b', '', name)
    name = re.sub(r'\s*\(\s*(?:Day\s+(?:One|Two|Three|Four|Five|Six|Seven|1|2|3|4|5|6|7)|\d{1,2}(?:st|nd|rd|th))\s*\)', '', name)
    name = re.sub(r'\s*\|\s*[A-Z]{2}:\s*\S+', '', name)
    name = re.sub(r'\s*-\s*\d{1,2}:\d{2}\s*$', '', name)
    return name.strip()


def smart_truncate(text: str, max_length: int = 100) -> str:
    if len(text) <= max_length:
        return text
    vs_match = re.search(r'.{0,45}\bvs\.?\b.{0,45}', text, re.IGNORECASE)
    if vs_match:
        truncated = vs_match.group(0).strip()
        if len(truncated) > 20:
            return truncated if len(truncated) <= max_length else truncated[:max_length - 3] + '...'
    truncated = text[:max_length - 3]
    last_space = truncated.rfind(' ')
    if last_space > max_length // 2:
        return truncated[:last_space] + '...'
    return text[:max_length - 3] + '...'


def clean_stream_name(raw_name: str) -> str:
    meaningful = extract_meaningful_title(raw_name)
    cleaned = meaningful
    for pattern in _COMPILED_CLEANUP:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    return smart_truncate(cleaned, max_length=100)


TZ_CONFIRMED = "confirmed"
TZ_UNCERTAIN = "uncertain"

# --- Country-flag detection (used by "country_flags": true) ---
# Explicit map for aliases and 3-letter codes; unknown 2-letter codes fall
# back to a computed regional-indicator flag in detect_country_flag().
_CODE_FLAG = {
    "ES": "🇪🇸", "ESP": "🇪🇸",
    "VO": "🇪🇸", "GO": "🇪🇸",
    "UK": "🇬🇧", "GB": "🇬🇧", "GBR": "🇬🇧",
    "US": "🇺🇸", "USA": "🇺🇸",
    "IT": "🇮🇹", "ITA": "🇮🇹",
    "DE": "🇩🇪", "DEU": "🇩🇪",
    "FR": "🇫🇷", "PT": "🇵🇹", "NL": "🇳🇱", "BE": "🇧🇪", "IE": "🇮🇪",
    "AU": "🇦🇺", "NZ": "🇳🇿", "CA": "🇨🇦", "MX": "🇲🇽",
    "BR": "🇧🇷", "AR": "🇦🇷", "JP": "🇯🇵", "KR": "🇰🇷", "CN": "🇨🇳", "IN": "🇮🇳",
    "GR": "🇬🇷", "GRE": "🇬🇷", "TR": "🇹🇷", "PL": "🇵🇱", "RU": "🇷🇺",
    "RO": "🇷🇴", "CZ": "🇨🇿", "HU": "🇭🇺", "SE": "🇸🇪", "NO": "🇳🇴",
    "DK": "🇩🇰", "FI": "🇫🇮", "CH": "🇨🇭", "AT": "🇦🇹", "HR": "🇭🇷",
    "RS": "🇷🇸", "BG": "🇧🇬", "SK": "🇸🇰", "SI": "🇸🇮", "CY": "🇨🇾",
    "IL": "🇮🇱", "SA": "🇸🇦", "AE": "🇦🇪", "QA": "🇶🇦", "EG": "🇪🇬",
    "ZA": "🇿🇦", "MA": "🇲🇦",
    "CL": "🇨🇱", "CO": "🇨🇴", "PE": "🇵🇪", "UY": "🇺🇾", "EC": "🇪🇨",
    "VE": "🇻🇪", "PY": "🇵🇾", "BO": "🇧🇴",
    "TH": "🇹🇭", "VN": "🇻🇳", "PH": "🇵🇭", "MY": "🇲🇾", "SG": "🇸🇬",
    "ID": "🇮🇩", "HK": "🇭🇰", "TW": "🇹🇼", "PK": "🇵🇰", "BD": "🇧🇩",
}

_PROVIDER_FLAG_PATTERNS = [
    (re.compile(r'\bTNT\s*SPORTS?\b', re.IGNORECASE), "🇬🇧"),
    (re.compile(r'\bSKY\s*UK\b', re.IGNORECASE),      "🇬🇧"),
    (re.compile(r'\bBT\s*SPORT\b', re.IGNORECASE),    "🇬🇧"),
    (re.compile(r'\bNOW\b', re.IGNORECASE),           "🇬🇧"),
    (re.compile(r'\bDAZN\s*ES\b', re.IGNORECASE),     "🇪🇸"),
    (re.compile(r'\bDAZN\s*IT\b', re.IGNORECASE),     "🇮🇹"),
    (re.compile(r'\bDAZN\s*DE\b', re.IGNORECASE),     "🇩🇪"),
    (re.compile(r'\bDAZN\s*FR\b', re.IGNORECASE),     "🇫🇷"),
    (re.compile(r'\bEspa[ñn]a\b|\bSpain\b', re.IGNORECASE),                  "🇪🇸"),
    (re.compile(r'\bUnited\s*Kingdom\b|\bReino\s*Unido\b', re.IGNORECASE),   "🇬🇧"),
    (re.compile(r'\bEstados\s*Unidos\b|\bUnited\s*States\b', re.IGNORECASE), "🇺🇸"),
    (re.compile(r'\bItalia\b|\bItaly\b', re.IGNORECASE),                     "🇮🇹"),
    (re.compile(r'\bAlemania\b|\bGermany\b', re.IGNORECASE),                 "🇩🇪"),
    (re.compile(r'\bESPN\b', re.IGNORECASE),          "🇺🇸"),
    (re.compile(r'\bFLSP\b', re.IGNORECASE),          "🇺🇸"),
    (re.compile(r'\bFLOR(?:ACING|UGBY)\b', re.IGNORECASE), "🇺🇸"),
    (re.compile(r'\bM\+'),                            "🇪🇸"),   # Movistar Plus (Spain)
    (re.compile(r'\bSTAN\b', re.IGNORECASE),          "🇦🇺"),
    (re.compile(r'\bTSN\b', re.IGNORECASE),           "🇨🇦"),
    (re.compile(r'\bRTE\b', re.IGNORECASE),           "🇮🇪"),
    (re.compile(r'\bbeIN\s*SPORTS\b', re.IGNORECASE), "🇶🇦"),
    (re.compile(r'\bSUPERSPORT\b', re.IGNORECASE),    "🇿🇦"),
]


def detect_country_flag(text: str) -> str:
    """
    Return a country flag emoji for explicit country signals in the text, or ''.
    Trusted signals: (1) known provider/brand keywords, (2) a leading country-code
    prefix such as 'ES:', 'UK|', 'US ('. Bare 2-letter words elsewhere are ignored.
    """
    if not text:
        return ""
    # A leading country-code prefix is the provider's own labeling and wins
    # over brand-keyword guesses (e.g. 'TR: BEIN SPORTS' is Turkey, not Qatar).
    m = re.match(r'^\s*([A-Z]{2,3})\s*(?:[:|]|\()', text)
    if m:
        code = m.group(1).upper()
        flag = _CODE_FLAG.get(code, "")
        if not flag and len(code) == 2 and code.isalpha():
            # Unknown 2-letter prefix: compute the regional-indicator flag
            # (renders as the country flag for any valid ISO code).
            flag = "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)
        if flag:
            return flag
    for pattern, flag in _PROVIDER_FLAG_PATTERNS:
        if pattern.search(text):
            return flag
    return ""


def detect_flag_from_stream(raw_stream_name: str, group_name: str, m3u_name: str) -> str:
    """Resolve a country flag from stream name, group name, then m3u name."""
    for comp in (raw_stream_name, group_name, m3u_name):
        flag = detect_country_flag(comp or "")
        if flag:
            return flag
    return ""


def build_name_channel_name(raw_stream_name: str,
                            stream: Dict,
                            hide_region_label: bool = False,
                            country_flags: bool = False) -> Tuple[str, Optional[datetime], str, str]:
    """
    Build the display name for a name-based (Phase 2) stream.
    Returns (display_name, sort_dt, tz_confidence, region_label).
    `stream` is a dict with keys: name, channel_group (name str), m3u_account_name.
    """
    # 1. extract embedded datetime
    extracted_local_dt, is_specific_date, _found_weekday = extract_datetime_from_stream_name(raw_stream_name)

    # 2. infer timezone — per-source priority, most specific first (same
    # order as detect_flag_from_stream): the stream's own name outranks its
    # group, which outranks the account. Scanning one concatenated blob let
    # a generic group token beat the name's explicit country prefix purely
    # by hint-table order (real case: stream "AU (STAN 03) | ..." in group
    # "US | STAN PPV" got US Eastern instead of Australia — while the flag,
    # correctly, showed 🇦🇺).
    group_name = stream.get("channel_group") or ""
    m3u_name = stream.get("m3u_account_name") or ""
    tz_result = (infer_timezone_from_text(raw_stream_name)
                 or infer_timezone_from_text(group_name)
                 or infer_timezone_from_text(m3u_name))

    # 3. clean the name
    clean_name = clean_stream_name(raw_stream_name)

    # 4. country flag
    flag = detect_flag_from_stream(raw_stream_name, group_name, m3u_name) if country_flags else ""
    flag_prefix = f"{flag} " if flag else ""

    # 5. build display time and confidence
    if extracted_local_dt and tz_result:
        tz_name, region_label = tz_result
        utc_dt = apply_timezone_offset(extracted_local_dt, tz_name)
        if utc_dt:
            local_dt = convert_utc_to_display(utc_dt)
            if is_specific_date:
                if flag:
                    region_part = ""
                else:
                    region_part = f" 🌍 {region_label}" if not hide_region_label else ""
                time_str = f"{local_dt.strftime('%H:%M')}, {local_dt.day}-{MONTHS_ES[local_dt.month - 1]}"
                display_name = f"{flag_prefix}{time_str}{region_part} | {clean_name}"
                return display_name, utc_dt, TZ_CONFIRMED, region_label
            else:
                time_str = f"{local_dt.strftime('%H:%M')}"
                display_name = f"{flag_prefix}{time_str} | {clean_name}"
                return display_name, utc_dt, TZ_CONFIRMED, ""

    if extracted_local_dt:
        # Time found but no timezone inferred → hide the unconvertible time.
        sort_dt_utc = convert_display_to_utc(extracted_local_dt)
        display_name = f"{flag_prefix}{clean_name}"
        return display_name, sort_dt_utc, TZ_UNCERTAIN, ""
    else:
        # No time at all → 24/7 always-on channel.
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        display_name = f"{flag_prefix}{clean_name}"
        return display_name, now_utc, TZ_UNCERTAIN, ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# The date-prefix produced by format_epg_channel_name/build_name_channel_name
# for a specific-date event: "HH:MM, D-Mon | Title". Named/exported so the
# channel-ownership adoption heuristic (runner.py) can recognize a
# plugin-generated channel name without duplicating this pattern.
GENERATED_NAME_DATE_RE = re.compile(r'(\d{2}):(\d{2}),\s+(\d{1,2})-([A-Za-z]{3})')


def is_channel_too_old(name: str, max_hours: float) -> bool:
    """
    Parse the timestamp from a channel name (e.g. '16:40, 13-Mar | Title')
    and check if it is older than max_hours.
    """
    try:
        match = GENERATED_NAME_DATE_RE.search(name)
        if not match:
            return False

        hour, minute, day, month_str = match.groups()
        try:
            month = MONTHS_ES.index(month_str) + 1
        except ValueError:
            return False

        now = datetime.now(_DISPLAY_TZ).replace(tzinfo=None)
        dt = datetime(now.year, month, int(day), int(hour), int(minute))

        # Handle year wrap-around (e.g. running in Jan against Dec events)
        if month == 12 and now.month == 1:
            dt = dt.replace(year=now.year - 1)
        elif month == 1 and now.month == 12:
            dt = dt.replace(year=now.year + 1)

        cutoff = now - timedelta(hours=max_hours)
        return dt < cutoff
    except Exception:
        return False


_WS_RE = re.compile(r"[\s  ​‌‍﻿]+")


def fold_text(s) -> str:
    """NFKD-fold accents (Fútbol -> match Futbol) and collapse every
    whitespace variant (including NBSP/zero-width chars scraped XMLTV
    sometimes contains) to a single ASCII space. Case-preserving --
    matching stays re.IGNORECASE; keep it that way so this is reusable
    by the event-identity code below, which lowercases separately.

    Side effect: NFKD folding also strips tildes/cedillas (ñ -> n, ç -> c),
    so "Peña" folds the same as "Pena". Deliberate: a rare false-positive
    match is far cheaper than a missed recording of a real game.
    """
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    normalized = unicodedata.normalize("NFC", stripped)
    return _WS_RE.sub(" ", normalized).strip()


def term_matches(term: str, *texts: str) -> bool:
    """Word-boundary term match used consistently by all phases.

    A term matches when it appears as a whole word (case-insensitive) in any of
    the given texts. This is the single "search dialect" shared by the exclude
    filter and the record filter, so the user configures one familiar syntax.
    Both the pattern and every candidate text are accent/whitespace-folded
    first via fold_text(), so "Futbol" matches "Fútbol" and NBSP-riddled
    scraped XMLTV titles compare cleanly. The lookaround boundary (rather than
    \\b...\\b) is deliberately more permissive -- e.g. it matches "##" inside
    "## Final" -- and is the one canonical matching dialect used everywhere.
    """
    term = (term or "").strip()
    if not term:
        return False
    folded_term = fold_text(term)
    if not folded_term:
        return False
    pattern = rf"(?<!\w){re.escape(folded_term)}(?!\w)"
    return any(re.search(pattern, fold_text(t), re.IGNORECASE) for t in texts)


def contains_normalized(term: str, text: str) -> bool:
    """Folded, case-insensitive SUBSTRING test (not word-boundary) -- used by
    the name-search phase, which needs substring semantics per its own
    help text."""
    term = (term or "").strip()
    if not term:
        return False
    return fold_text(term).lower() in fold_text(text or "").lower()


def prefix_matches(prefix: str, text: str) -> bool:
    """Folded startswith() test."""
    prefix = (prefix or "").strip()
    if not prefix:
        return False
    return fold_text(text or "").lower().startswith(fold_text(prefix).lower())


def exclude_matches(term: str, *texts: str) -> bool:
    """Word-boundary exclusion check used consistently by all phases."""
    return term_matches(term, *texts)


def record_matches(patterns: List[str], excludes: List[str], *texts: str) -> bool:
    """Opt-in record filter for the DVR feature.

    Returns True only when a title matches at least one ``patterns`` term and
    none of the ``excludes`` terms. An empty ``patterns`` list records nothing
    (recording is strictly opt-in — dozens of event channels per day must not
    auto-record). Uses the exact same whole-word dialect as ``exclude_matches``.
    """
    if not patterns:
        return False
    if not any(term_matches(p, *texts) for p in patterns):
        return False
    if excludes and any(term_matches(x, *texts) for x in excludes):
        return False
    return True


# ---------------------------------------------------------------------------
# Event identity + recording reconciliation
#
# These primitives make dedup/extend/reschedule decisions independent of
# cosmetic display-name formatting (flag emoji, region label, date-prefix
# toggles) so the keyword-EPG path and the teamarr-watch path converge on the
# same identity for the same real-world event.
# ---------------------------------------------------------------------------

def normalize_event_title(title) -> str:
    """Canonical form of an event title for identity purposes -- fold,
    casefold, collapse whitespace. Used to build a stable key that
    survives cosmetic display-formatting differences (flag emoji,
    date-prefix toggles) between the two recording-creation paths."""
    return fold_text(title or "").casefold()


def event_identity(title, event_iso: str) -> str:
    """Stable event identity: normalized raw title + exact ISO start.
    Must always be fed the RAW underlying title (raw EPG programme
    title, or clean_stream_name's output for the name-search path) --
    NEVER a formatted display name (format_epg_channel_name /
    build_name_channel_name output), or per-job cosmetic toggles like
    country_flags / no_region_label will silently break dedup between
    two jobs or two runs. This is what makes the keyword-EPG path and
    the teamarr-watch path converge on the same identity for the same
    real-world event."""
    return f"{normalize_event_title(title)}|{event_iso}"


RECONCILE_CREATE = "create"
RECONCILE_SKIP = "skip"
RECONCILE_EXTEND = "extend"
RECONCILE_RESCHEDULE = "reschedule"
RECONCILE_SHIFT_LIVE = "shift_live"
RECONCILE_TOMBSTONE = "tombstone"

_TERMINAL_STATUSES = {"completed", "stopped", "interrupted", "failed"}
# Public alias -- runner.py's concurrency-cap count reads this from outside
# the module.
TERMINAL_STATUSES = _TERMINAL_STATUSES


def decide_recording_action(desired, existing, tombstones, now,
                             shift_tolerance_hours=3.0,
                             max_extension_hours=2.0,
                             legacy_keys=()):
    """Pure decision function -- no Django, no I/O, fully unit-testable.

    desired: dict with keys "title_norm" (via normalize_event_title),
        "event_iso" (str), "event_dt" (aware datetime), "start" (aware
        datetime), "end" (aware datetime).
    existing: list of plain dicts, each: "pk", "title_norm", "event_iso",
        "event_dt" (aware datetime), "start" (aware datetime),
        "end" (aware datetime), "status" (str, may be ""), "event_key"
        (str, the row's own stored identity key), "channel_id".
    tombstones: set of key strings the user has deliberately deleted.
    now: aware datetime.
    legacy_keys: iterable of extra key strings that should ALSO be
        treated as an exact match against `desired` (used during the
        version transition so pre-upgrade rows, keyed on the old
        display-name-based scheme, aren't falsely treated as new).

    Returns (action, target_pk_or_None, adjusted_end_or_None, reason_str)
    where action is one of the RECONCILE_* constants above.
    """
    desired_key = f"{desired['title_norm']}|{desired['event_iso']}"
    all_exact_keys = {desired_key, *legacy_keys}

    # 1. Exact key match against any existing row's own event_key,
    #    OR a key re-derived from that row's stored title_norm+event_iso
    #    (covers legacy rows that never got an event_key field).
    for row in existing:
        row_key = row.get("event_key") or f"{row['title_norm']}|{row['event_iso']}"
        if row_key in all_exact_keys:
            if row["status"] in _TERMINAL_STATUSES:
                return (RECONCILE_SKIP, row["pk"], None, "matched row is terminal, not resurrecting")
            if row["end"] >= desired["end"]:
                return (RECONCILE_SKIP, row["pk"], None, "already covers the desired window")
            original_end = row.get("original_end", row["end"])
            adjusted = min(desired["end"], original_end + timedelta(hours=max_extension_hours)) \
                if max_extension_hours > 0 else desired["end"]
            return (RECONCILE_EXTEND, row["pk"], adjusted, "later end now known")

    # 2. Tolerance-window match: same title, different but nearby time,
    #    not a terminal row.
    if shift_tolerance_hours > 0:
        candidates = [
            row for row in existing
            if row["title_norm"] == desired["title_norm"]
            and row["status"] not in _TERMINAL_STATUSES
            and abs((row["event_dt"] - desired["event_dt"]).total_seconds()) <= shift_tolerance_hours * 3600
        ]
        if candidates:
            nearest = min(candidates, key=lambda r: abs((r["event_dt"] - desired["event_dt"]).total_seconds()))
            if nearest["status"] == "recording" or nearest["start"] <= now:
                return (RECONCILE_SHIFT_LIVE, nearest["pk"], desired["end"], "event time shifted while already capturing")
            return (RECONCILE_RESCHEDULE, nearest["pk"], desired["end"], "event time shifted, row still pending")

    # 3. Tombstone.
    if desired_key in tombstones or any(k in tombstones for k in legacy_keys):
        return (RECONCILE_TOMBSTONE, None, None, "user previously deleted this recording")

    return (RECONCILE_CREATE, None, None, "no match")


# ---------------------------------------------------------------------------
# Black-screen stream detection (EPG phase only)
#
# ffmpeg's signalstats filter reports a per-frame average luma (YAVG) via the
# metadata=print muxer. In YUV TV range 16 is pure black and real content sits
# well above 40, so a low mean YAVG over a few seconds means the stream is a
# permanent black screen. These helpers stay Django-/subprocess-free: runner.py
# runs ffmpeg and feeds its stderr here for parsing/classification.
# ---------------------------------------------------------------------------

YAVG_BLACK_THRESHOLD = 20.0

PROBE_BLACK = "black"
PROBE_GOOD = "good"
PROBE_INDETERMINATE = "indeterminate"

_YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([\d.]+)")


def parse_yavg_samples(ffmpeg_stderr: str) -> List[float]:
    """Extract every ``lavfi.signalstats.YAVG=<n>`` value from ffmpeg stderr."""
    samples: List[float] = []
    for m in _YAVG_RE.finditer(ffmpeg_stderr or ""):
        try:
            samples.append(float(m.group(1)))
        except ValueError:
            continue
    return samples


def classify_probe(samples: List[float],
                   threshold: float = YAVG_BLACK_THRESHOLD) -> Tuple[str, Optional[float]]:
    """
    Classify a stream probe from its YAVG samples.

    Returns ``(verdict, mean)`` where verdict is one of PROBE_BLACK / PROBE_GOOD
    / PROBE_INDETERMINATE and mean is the average YAVG (None when there are no
    samples). No samples → indeterminate; mean < threshold → black; else good.
    """
    if not samples:
        return PROBE_INDETERMINATE, None
    mean = sum(samples) / len(samples)
    if mean < threshold:
        return PROBE_BLACK, mean
    return PROBE_GOOD, mean


# ffmpeg prints the root cause (HTTP status, connection error, missing video
# stream) well before its generic trailing "Error opening output files" line —
# prefer the first line that looks like an actual error.
_FFMPEG_ERROR_LINE_RE = re.compile(
    r"(?i)\b(error|failed|refused|forbidden|denied|not found|timed? ?out|"
    r"unauthorized|invalid data|4\d\d|5\d\d)\b")


def pick_error_line(ffmpeg_stderr: str, max_len: int = 160) -> str:
    """The most informative error line of an ffmpeg stderr dump ('' if none)."""
    lines = [ln.strip() for ln in (ffmpeg_stderr or "").splitlines() if ln.strip()]
    for ln in lines:
        if _FFMPEG_ERROR_LINE_RE.search(ln):
            return ln[:max_len]
    return lines[-1][:max_len] if lines else ""


# ---------------------------------------------------------------------------
# Channel numbering (v1.3.0)
#
# Dispatcharr's own build_reserved_set() (apps/channels/compact_numbering.py)
# reserves BOTH Channel.channel_number and ChannelOverride.channel_number
# instance-wide -- NumberAllocator mirrors that exactly. Numbers are floats
# throughout Dispatcharr, so every number is normalized through float() on
# the way in/out (260 and 260.0 must be treated identically).
# ---------------------------------------------------------------------------

class NumberAllocator:
    """Pure channel-number allocator, no I/O.

    ``used``: every channel_number/ChannelOverride.channel_number currently
    in the DB (instance-wide -- Dispatcharr enforces number uniqueness
    instance-wide, not just per-group).
    ``blocked_ranges``: (lo, hi, owner_job_name) for every OTHER enabled
    job's configured numbering span -- a number inside one of these is off
    limits to any job that doesn't own that range. ``hi`` may be None for an
    unbounded (highest-start, no end_number) job.
    """

    def __init__(self, used, blocked_ranges):
        self._used = {self._norm(n) for n in used if n is not None}
        self._blocked = [(self._norm(lo), (self._norm(hi) if hi is not None else None), owner)
                         for lo, hi, owner in (blocked_ranges or [])]

    @staticmethod
    def _norm(n) -> float:
        return float(n)

    def release(self, number) -> None:
        if number is None:
            return
        self._used.discard(self._norm(number))

    def reserve(self, number) -> None:
        if number is None:
            return
        self._used.add(self._norm(number))

    def _blocked_for(self, n: float, requesting_job: str) -> bool:
        for lo, hi, owner in self._blocked:
            if owner == requesting_job:
                continue  # a job's own span never blocks itself
            if n >= lo and (hi is None or n <= hi):
                return True
        return False

    # Hard cap on candidate numbers checked per next_free() call, independent
    # of ``end`` (which may be None/unbounded, e.g. two jobs tied on the same
    # start_number with no end_number -- see compute_job_number_spans). No
    # real Dispatcharr instance has anywhere near this many channel numbers
    # in a single job's realistic range, so this can never fire in normal
    # use -- it exists purely so a misconfigured/unbounded span can never
    # hang the allocator (and, via the run-lock's finally, every future
    # scheduled run) instead of returning an error.
    _MAX_CANDIDATES = 100_000

    def next_free(self, cursor, end, requesting_job: str):
        """Walk upward from ``cursor`` (inclusive) for the next number that is
        neither used nor inside another job's blocked range. Returns
        ``(number, reason)`` on success (``reason`` starts with "skipped "
        when one or more occupied/blocked numbers had to be stepped over,
        else "ok"), or ``(None, reason)`` if `end` is reached first (`end`
        may be None for an unbounded search, in which case exhaustion never
        happens) OR the search bound (see ``_MAX_CANDIDATES``) is reached
        first -- this guarantees the call always terminates, regardless of
        how ``end`` or the blocked ranges are configured. Does NOT reserve
        the returned number -- the caller reserves explicitly only after
        actually creating the channel.
        """
        if cursor is None:
            return None, "no starting cursor"
        n = self._norm(cursor)
        end_n = self._norm(end) if end is not None else None
        skipped = []
        checked = 0
        while end_n is None or n <= end_n:
            if checked >= self._MAX_CANDIDATES:
                return None, ("exhausted search bound without finding a free "
                               f"number (checked {checked} candidates from {cursor})")
            checked += 1
            if n in self._used or self._blocked_for(n, requesting_job):
                skipped.append(n)
                n += 1.0
                continue
            if skipped:
                return n, f"skipped {len(skipped)} occupied/blocked number(s): {skipped}"
            return n, "ok"
        return None, f"range exhausted (no free number <= {end_n})"


def decide_channel_purge(ch: dict, owner, job_name: str, known_job_names: set,
                         is_preserved: bool):
    """Pure ownership/purge policy. Returns (delete_eligible, reason).

    ``is_preserved`` wins over everything (never delete). An unowned channel
    (no registry entry) is never deleted -- protects manual channels and,
    pre-adoption, everything the plugin already made. A channel owned by
    another currently-configured job (enabled or not) is never touched by
    this job. A channel owned by a job name that no longer exists in the
    config is delete-eligible AND flagged for adoption into the current job
    (the reason string names it) -- the caller still applies its own
    purge_group/purge_unmatched/cleanup rules on top of "delete-eligible".
    """
    if is_preserved:
        return False, "preserved"
    if owner is None:
        return False, "unowned, protected"
    if owner == job_name:
        return True, "owned by this job"
    if owner in known_job_names:
        return False, f"owned by job '{owner}'"
    return True, f"adopting from removed job '{owner}'"


def _channel_disambiguation_score(desired: dict, candidate: dict) -> int:
    """Lower is a better match: exact stream-set match, then any shared
    stream id, then equal slot index, else no particular preference."""
    desired_ids = set(desired.get("stream_ids") or [])
    cand_ids = set(candidate.get("stream_ids") or [])
    if desired_ids and cand_ids and desired_ids == cand_ids:
        return 0
    if desired_ids & cand_ids:
        return 1
    if desired.get("slot") is not None and candidate.get("slot") == desired.get("slot"):
        return 2
    return 3


def decide_channel_action(desired: dict, owned_candidates: list, shift_tolerance_hours: float):
    """Pure update-in-place matching decision (Phase 1 only).

    ``desired``: {title_norm, event_iso, event_dt, stream_ids, slot}.
    ``owned_candidates``: this job's owned channels in the target group, each
    a dict of the same shape plus channel_id. The caller is responsible for
    removing a candidate from this list once it has been claimed by an
    earlier ``desired`` entry in the same pass, so no candidate is ever
    claimed twice.

    Returns (action, target_channel_id_or_None, reason) where action is
    "update" or "create". Priority: (1) exact title_norm+event_iso match
    (disambiguated by stream-set/shared-id/slot when several candidates tie
    on title+time -- split-mode); (2) same title_norm within
    shift_tolerance_hours of event_dt, nearest wins (ties broken the same
    way); (3) create.
    """
    exact = [c for c in owned_candidates
             if c.get("title_norm") == desired.get("title_norm")
             and c.get("event_iso") == desired.get("event_iso")]
    if exact:
        best = min(exact, key=lambda c: _channel_disambiguation_score(desired, c))
        return "update", best["channel_id"], "exact identity match"

    if shift_tolerance_hours and shift_tolerance_hours > 0:
        tol_seconds = shift_tolerance_hours * 3600
        candidates = [
            c for c in owned_candidates
            if c.get("title_norm") == desired.get("title_norm")
            and c.get("event_dt") is not None and desired.get("event_dt") is not None
            and abs((c["event_dt"] - desired["event_dt"]).total_seconds()) <= tol_seconds
        ]
        if candidates:
            best = min(candidates, key=lambda c: (
                abs((c["event_dt"] - desired["event_dt"]).total_seconds()),
                _channel_disambiguation_score(desired, c)))
            return "update", best["channel_id"], "event time shifted within tolerance"

    return "create", None, "no match"


def epg_link_is_trustworthy(candidate_programmes: list, identity_title: str,
                            sort_dt, tolerance_minutes: float):
    """Phase 2 EPG corroboration (Issue 4a). Returns (trustworthy, reason).

    ``candidate_programmes``: list of {"start_time": datetime, "title": str}
    already fetched by the caller from ProgramData near sort_dt. Trustworthy
    only when BOTH a start-time match within tolerance_minutes of sort_dt AND
    a title match are found on the SAME programme -- either alone is not
    enough (a provider's self-reported tvg_id on a name-search stream is
    otherwise likely to resolve to its generic 24/7 channel guide, which
    would attach a totally unrelated full-day programme purely because the
    times happen to line up).
    """
    if not candidate_programmes:
        return False, "no programmes found near the event time"
    if sort_dt is None:
        return False, "no reliable event time to corroborate against"

    tolerance = timedelta(minutes=tolerance_minutes)
    best_reason = "no programme within the time tolerance"
    for p in candidate_programmes:
        start = p.get("start_time")
        if start is None:
            continue
        try:
            within_time = abs(start - sort_dt) <= tolerance
        except TypeError:
            continue
        if not within_time:
            continue
        cand_title = p.get("title") or ""
        if not cand_title:
            best_reason = "time matched but the programme has no title to corroborate"
            continue
        title_ok = (normalize_event_title(identity_title) == normalize_event_title(cand_title)
                   or contains_normalized(identity_title, cand_title)
                   or contains_normalized(cand_title, identity_title))
        if title_ok:
            return True, "time and title corroborated"
        best_reason = f"time matched but title did not corroborate (programme title: '{cand_title}')"
    return False, best_reason


def probe_budget_plan(total: int, job_names: list):
    """Per-job black-screen probe budget with a shared surplus pool.

    Returns (per_job_plan, surplus) where per_job_plan is
    {job_name: {"reserve": int, "used": 0, "warned": False}} and surplus is
    the leftover from integer division, spent first-come by any job whose
    own reserve runs out. total<=0 disables probing (every reserve is 0).
    Handles an empty job_names list (no division by zero) and a single job
    (reserve = total, no surplus).
    """
    total = max(int(total), 0)
    plan = {name: {"reserve": 0, "used": 0, "warned": False} for name in job_names}
    n = len(job_names)
    if n == 0:
        return plan, total
    reserve = total // n
    surplus = total - reserve * n
    for name in job_names:
        plan[name]["reserve"] = reserve
    return plan, surplus
