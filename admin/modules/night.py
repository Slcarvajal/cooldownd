"""Night mode: when pushes and kiosk chimes keep quiet, and which night a moment belongs to.

Not a card. Each panel reads its own config "night": {"bedtime": "23:30", "wake": "06:30"} (local time,
24 h). The Pi and the hub keep separate copies of that key, so change both or they drift apart.
Without the key, or with a time that doesn't parse, or with bedtime == wake, there is no night:
quiet() is False and the others return None.

A night runs from bedtime (included) to the next wake (excluded); it may cross midnight. It is named by
its evening: the "YYYY-MM-DD" date of the day it hangs off. A bedtime at or after noon starts on the
evening itself (23:30 on 2026-10-10 is the night of 2026-10-10); one before noon starts the day after
(01:00 on 2026-10-11 is also the night of 2026-10-10).

- window(cfg) -> (bedtime, wake) as datetime.time, or None
- quiet(cfg, now=None) -> bool; now is a Unix timestamp (default: time.time())
- night_of(ts, cfg) -> "YYYY-MM-DD" evening of the night that holds timestamp ts, or None outside one
- bounds(evening, cfg) -> (start_ts, end_ts) floats of that night, or None; evening is "YYYY-MM-DD" or a date
- next_wake(cfg, now) -> timestamp (float) of the first wake strictly after now, or None"""
import re
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

NOON = dtime(12, 0)


def _clock(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s or "").strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return None
    return dtime(int(m[1]), int(m[2]))


def window(cfg):
    c = (cfg or {}).get("night") or {}
    bed, wake = _clock(c.get("bedtime")), _clock(c.get("wake"))
    if bed is None or wake is None or bed == wake:
        return None
    return bed, wake


def bounds(evening, cfg):
    w = window(cfg)
    if not w:
        return None
    if isinstance(evening, str):
        evening = date.fromisoformat(evening)
    bed, wake = w
    start_day = evening if bed >= NOON else evening + timedelta(days=1)
    end_day = start_day if wake > bed else start_day + timedelta(days=1)
    # Naive datetimes are local time, so a DST change shifts the timestamps the way the clock does.
    return datetime.combine(start_day, bed).timestamp(), datetime.combine(end_day, wake).timestamp()


def night_of(ts, cfg):
    if not window(cfg):
        return None
    day = datetime.fromtimestamp(ts).date()
    # The night holding ts hangs off today, yesterday or (for a pre-noon bedtime) the day before.
    for back in (2, 1, 0):
        evening = day - timedelta(days=back)
        start, end = bounds(evening, cfg)
        if start <= ts < end:
            return evening.isoformat()
    return None


def quiet(cfg, now=None):
    return night_of(time.time() if now is None else now, cfg) is not None


def next_wake(cfg, now):
    w = window(cfg)
    if not w:
        return None
    day = datetime.fromtimestamp(now).date()
    for ahead in range(3):
        t = datetime.combine(day + timedelta(days=ahead), w[1]).timestamp()
        if t > now:
            return t
    return None
