"""Phone alerts from the panel itself (Web Push, modules/webpush.py): agents that wait on you, and
servers that go down (modules/uptime.py calls send()). No other app: the iPhone gets them from the
panel added to its Home Screen, after "Turn on alerts" on this card.

config "notify": {"panel", "after_seconds"}
An agent is sent once per wait, after it has waited `after_seconds`, so one you answer at the PC
right away never buzzes the phone. Tapping the alert opens that agent on the panel."""
import time
import urllib.parse

from modules import webpush
from modules.common import STATE, ago, e, row

NAME, TITLE, TAB = "notify", "Phone alerts", "machine"
SAMPLE_EVERY = 10
_seen = {}   # pager key -> first time it was seen waiting
_sent = set()
_log = {"last": None, "error": None, "count": 0}


def _conf(cfg):
    return cfg.get("notify") or {}


def send(cfg, title, body, url=None, tag=None, urgency="high"):
    """Push one alert to every device; False (and the error on the card) when none got it."""
    c = _conf(cfg)
    panel = c.get("panel", "")
    msg = {"title": title, "body": body or "", "url": url or panel, "tag": tag}
    try:
        delivered, errors = webpush.send_all(msg, subject=panel, urgency=urgency)
    except Exception as ex:  # a broken key file shouldn't kill the sampler
        delivered, errors = 0, [f"{type(ex).__name__}: {ex}"]
    _log["error"] = f"{time.strftime('%H:%M')} " + "; ".join(errors) if errors else None
    if delivered:
        _log.update(last=time.time(), count=_log["count"] + 1)
        print(f"notify: {title} → {delivered} device(s)", flush=True)
    return bool(delivered)


def _open_link(cfg, sheet, title):
    return _conf(cfg).get("panel", "") + "#" + urllib.parse.urlencode({"sheet": sheet, "title": title})


def _waiting(cfg):
    from modules import remote
    from modules.agents import key_of
    local = [{"key": key_of(a), "sheet": f"agents/view/{a['handle']}", "title": a["title"],
              "project": a["project"], "summary": a["summary"]}
             for a in (STATE.get("agents") or {}).get("items", []) if a["status"] == "waiting"]
    return local + remote.pager(cfg)


def sample(cfg):
    c = _conf(cfg)
    if not c or not webpush.subscriptions():
        return
    now, waiting = time.time(), _waiting(cfg)
    keys = {a["key"] for a in waiting}
    for k in list(_seen):
        if k not in keys:  # answered or moved on: a later wait alerts again
            _seen.pop(k)
            _sent.discard(k)
    for a in waiting:
        first = _seen.setdefault(a["key"], now)
        if a["key"] in _sent or now - first < c.get("after_seconds", 60):
            continue
        if send(cfg, f"{a['title']} needs you", f"{a['project']}: {a['summary']}".strip(": "),
                url=_open_link(cfg, a["sheet"], f"{a['title']} · {a['project']}"), tag=a["sheet"]):
            _sent.add(a["key"])


def card(cfg):
    c = _conf(cfg)
    devices = webpush.subscriptions()
    out = [f'<button data-push="on" data-post="notify/subscribe" data-vapid="{e(webpush.public_key())}">Turn on alerts on this device</button>',
           '<small class="muted">iPhone: open this panel from its Home Screen icon first (Share → Add to Home Screen).</small>',
           row("Sends", e(f"agents waiting {c.get('after_seconds', 60)} s · servers down and back"))]
    for i, d in enumerate(devices):
        out.append(row(d["label"], f'<small>since {e(d["added"])}</small> <button class="mini" data-action="notify/forget/{i}" '
                                   f'data-confirm="Stop alerts on {e(d["label"])}?">remove</button>'))
    if devices:
        last = f"last {ago(time.time() - _log['last'])} ago" if _log["last"] else "none yet"
        out.append(row("Sent", e(f"{_log['count']} since start · {last}") +
                       ' <button class="mini" data-action="notify/test">Send a test</button>'))
    else:
        out.append(row("Devices", '<span class="muted">none yet</span>'))
    if _log["error"]:
        out.append(f'<div class="alert bad">{e(_log["error"][:300])}</div>')
    return "".join(out)


def subscribe(cfg, arg, body):
    webpush.add(body.get("sub") or {}, str(body.get("label") or "Device"))
    print(f"notify: subscribed {body.get('label')}", flush=True)
    return "Alerts are on for this device."


def forget(cfg, arg, body):
    devices = webpush.subscriptions()
    d = devices[int(arg)]
    webpush.remove(d["endpoint"])
    return f"Alerts are off for {d['label']}."


def test(cfg, arg, body):
    ok = send(cfg, "Test from the control center", "Phone alerts work.", tag="test")
    return "Sent. Check your phone." if ok else f"No device got it: {_log['error']}"


ACTIONS = {"subscribe": subscribe, "forget": forget, "test": test}
