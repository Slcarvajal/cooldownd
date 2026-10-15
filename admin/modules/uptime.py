"""Production servers, checked from home every minute; a phone alert when one goes down and
when it's back (modules/notify.py).

config "uptime": {"fail_after", "checks": [{"name", "url", "expect"?, "contains"?}]}
A check is up when the status is `expect` (200 by default) and the body has `contains`, if set.
It alerts after `fail_after` failures in a row (2 by default), so one dropped request doesn't buzz."""
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from modules import notify
from modules.common import STATE, ago, e, pill, row

NAME, TITLE, TAB = "uptime", "Servers", "home"
SAMPLE_EVERY = 60
TIMEOUT = 10


def _probe(check):
    """(ok, detail, ms)."""
    t0 = time.monotonic()
    req = urllib.request.Request(check["url"], headers={"User-Agent": "homelab-uptime"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            code, body = r.status, r.read(4096).decode(errors="replace")
    except urllib.error.HTTPError as ex:
        code, body = ex.code, ex.read(4096).decode(errors="replace")
    except (OSError, urllib.error.URLError) as ex:
        return False, str(getattr(ex, "reason", ex))[:80], None
    ms = (time.monotonic() - t0) * 1000
    if code != check.get("expect", 200):
        return False, f"HTTP {code}", ms
    if check.get("contains") and check["contains"] not in body:
        return False, "unexpected answer", ms
    return True, "", ms


def sample(cfg):
    conf = cfg.get("uptime") or {}
    checks = conf.get("checks", [])
    if not checks:
        return
    prev = STATE.get("uptime", {})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_probe, checks))
    now, state = time.time(), {}
    for c, (ok, detail, ms) in zip(checks, results):
        p = prev.get(c["name"], {"fails": 0, "since": now, "alerted": False, "ok": True})
        s = {"ok": ok, "detail": detail, "ms": ms, "fails": 0 if ok else p["fails"] + 1,
             "since": p["since"] if ok == p["ok"] else now, "alerted": p["alerted"], "url": c["url"]}
        if not ok and not s["alerted"] and s["fails"] >= conf.get("fail_after", 2):
            s["alerted"] = notify.send(cfg, f"🔴 {c['name']} is down", f"{detail} · {c['url']}", tag=f"uptime {c['name']}")
        elif ok and s["alerted"]:
            down = ago(now - p["since"])
            if notify.send(cfg, f"🟢 {c['name']} is back", f"It was down for {down}.", tag=f"uptime {c['name']}",
                           urgency="normal"):
                s["alerted"] = False
        state[c["name"]] = s
    STATE["uptime"] = state


def card(cfg):
    checks = (cfg.get("uptime") or {}).get("checks", [])
    if not checks:
        return '<p class="muted">No servers listed in config "uptime".</p>'
    state, now = STATE.get("uptime", {}), time.time()
    out = []
    for c in checks:
        s = state.get(c["name"])
        if s is None:
            right = pill("dim", "checking…")
        elif s["ok"]:
            right = f'<small>{s["ms"]:.0f} ms</small> {pill(True, "up " + ago(now - s["since"]))}'
        else:
            lvl = "bad" if s["fails"] >= (cfg.get("uptime") or {}).get("fail_after", 2) else "warn"
            right = f'<small>{e(s["detail"])}</small> {pill(lvl, "down " + ago(now - s["since"]))}'
        out.append(row(c["name"], right))
    return "".join(out)
