"""How long this desktop's keyboard and mouse have been idle, from GNOME (Mutter's IdleMonitor).

Not a card. Needs `gdbus` and a session bus; the panel's systemd user unit has DBUS_SESSION_BUS_ADDRESS.

- parse(out) -> seconds (float) from gdbus output such as "(uint64 12345,)" (milliseconds), or None
- idle_seconds() -> seconds (float), or None when gdbus fails or isn't there. Cached for 2 s, since
  several samplers ask within the same moment."""
import re
import time

from modules.common import run

CMD = ("gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
       "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
       "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime")
CACHE_S = 2

_cache = {}


def parse(out):
    m = re.fullmatch(r"\(uint64\s+(\d+),\)", (out or "").strip())
    return int(m[1]) / 1000 if m else None


def idle_seconds():
    hit = _cache.get("idle")
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    secs = parse(run(*CMD, timeout=2))
    # A failure is cached too, so a missing bus doesn't cost a process per caller.
    _cache["idle"] = (time.monotonic(), secs)
    return secs
