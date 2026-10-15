"""Cards from another panel (the desktop's), shown inside this one.

config.json:
  "remotes": {"desk": {"url": "https://<host>.ts.net:8443", "name": "Desktop"}}
  "remote_modules": {"desk.agents": {"title": "Agents", "tab": "agents"}, ...}
and list "desk.agents" in "modules". The card is the remote's own HTML with its
data-action / data-sheet / data-post targets (and file/ image URLs) prefixed "desk.",
so clicks come back here and are forwarded (data-photo and data-page too). Its waiting agents join this panel's pager.
`tailscale serve` on the remote adds the login header for this machine's owner, so the
remote's allowed_users check still applies; a tagged machine with no owner (the Switch)
sends the shared hub token instead.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

from modules.common import e, hub_token

TIMEOUT = 4
ACTION_TIMEOUT = 60  # an agent send takes up to 30 s; a slow action on a remote, more
FRESH = 2.5  # seconds a remote /live result is reused across cards and clients

_live = {}  # remote id -> (fetched_at, cards dict or None, pager list)
_locks = {}
_HOOKS = re.compile(r'(data-(?:action|sheet|post|photo|page)="|src="file/)')


def _fetch(cfg, rid, path, body=None, timeout=TIMEOUT):
    """(content type, bytes) from the remote panel."""
    url = cfg["remotes"][rid]["url"].rstrip("/") + "/" + path.lstrip("/")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json", "X-Hub-Token": hub_token()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("Content-Type", ""), r.read()


def _request(cfg, rid, path, body=None, timeout=TIMEOUT):
    return _fetch(cfg, rid, path, body, timeout)[1]


def live(cfg, rid):
    """(cards, pager) from the remote's /live, or (None, []) when it's unreachable."""
    lock = _locks.setdefault(rid, threading.Lock())
    with lock:  # one fetch per refresh, however many cards and phones ask
        hit = _live.get(rid)
        if hit and time.monotonic() - hit[0] < FRESH:
            return hit[1], hit[2]
        try:
            d = json.loads(_request(cfg, rid, "live"))
            cards = d.get("cards", {})
            # waiting agents, re-addressed so the pager opens them through this panel
            pager = [{**a, "key": f"{rid}.{a['key']}", "sheet": f"{rid}." + a.get("sheet", f"agents/view/{a.get('handle')}")}
                     for a in d.get("pager", [])]
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            cards, pager = None, []
        _live[rid] = (time.monotonic(), cards, pager)
        return cards, pager


def pager(cfg):
    """Waiting agents from remotes, only those whose agents card this panel shows. The Switch reads
    the desktop and the Pi, and the Pi's pager already holds the desktop's agents: page them once."""
    shown = set(cfg.get("modules", []))
    used = {m.split(".", 1)[0] for m in shown if "." in m}
    return [a for rid in cfg.get("remotes", {}) if rid in used for a in live(cfg, rid)[1]
            if a["sheet"].split("/", 1)[0] in shown]


def _prefix(rid, html):
    return _HOOKS.sub(lambda m: f'{m.group(1)}{rid}.', html)


def _offline(cfg, rid):
    name = cfg["remotes"][rid].get("name", rid)
    return f'<p class="muted">{e(name)} is asleep or unreachable. Wake it from Home.</p>'


class _Forward(dict):
    """ACTIONS / SHEETS stand-in: any name forwards to the remote."""

    def __init__(self, rid, module, kind):
        super().__init__()
        self.rid, self.module, self.kind = rid, module, kind

    def get(self, name, default=None):
        def call(cfg, arg, body=None):
            path = f"{self.kind}/{self.module}/{name}" + (f"/{arg}" if arg is not None else "")
            try:
                if self.kind == "sheet":
                    return _prefix(self.rid, _request(cfg, self.rid, path).decode())
                if self.kind == "page":  # full pages address their module from their own URL
                    return _request(cfg, self.rid, path, timeout=10).decode()
                if self.kind == "file":
                    return _fetch(cfg, self.rid, path, timeout=10)
                print(f"remote {self.rid} {path}", flush=True)
                return json.loads(_request(cfg, self.rid, path, body or {}, timeout=ACTION_TIMEOUT))
            except urllib.error.HTTPError as ex:
                msg = ex.read().decode(errors="replace")
                try:
                    msg = json.loads(msg).get("message", msg)
                except ValueError:
                    pass
                raise RuntimeError(msg) from None
            except (OSError, urllib.error.URLError):
                raise RuntimeError(f"{cfg['remotes'][self.rid].get('name', self.rid)} is unreachable") from None
        return call


class _AnySize(dict):
    """UPLOADS stand-in: the remote enforces its own limits."""

    def get(self, name, default=None):
        return 8 << 20


class Remote:
    UPLOADS = _AnySize()

    def __init__(self, full, cfg):
        self.NAME = full
        self.rid, self.module = full.split(".", 1)
        spec = cfg.get("remote_modules", {}).get(full, {})
        self.TITLE = spec.get("title", self.module.title())
        self.TAB = spec.get("tab", "home")
        self.ACTIONS = _Forward(self.rid, self.module, "action")
        self.SHEETS = _Forward(self.rid, self.module, "sheet")
        self.FILES = _Forward(self.rid, self.module, "file")
        self.PAGES = _Forward(self.rid, self.module, "page")

    def card(self, cfg):
        cards, _ = live(cfg, self.rid)
        if cards is None:
            return _offline(cfg, self.rid)
        return _prefix(self.rid, cards.get(self.module, '<p class="muted">Not on that panel.</p>'))
