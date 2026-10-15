"""Co-op (on the Pi): a weekly quest board shared with friends who run this same panel.

Off until it's set up: without ~/.config/homelab-admin/friends.json the board stays empty, and the
share listener only starts with SHARE_PORT (app.py ShareHandler, never Funnel).

- Sharing: GET /share/v1/week on that listener answers serve(), only to a friend's share token. Only
  summary() leaves: name, level, title, this week's quests and boss. No meals, sleep or agents.
  ~/.config/homelab-admin/share-tokens.json (600) keeps {friend: sha256 of the token}, never the token.
  CLIENT_FAIL_LIMIT bad tokens in FAIL_WINDOW seconds from one client (its Tailscale-User-Login, else
  its address) answer 429 to that client; FAIL_LIMIT from everyone together answer 429 to everyone.
- Reading: every SAMPLE_EVERY seconds the friends in ~/.config/homelab-admin/friends.json (600,
  {friend: {"url": "https://<their pi>.ts.net:<port>", "token"}}) are read with an opener of our own:
  never remote.py's, so the hub token never leaves; no redirects, https on *.ts.net only, TIMEOUT
  seconds and MAX_BYTES. Every answer goes through clean(). The board lands in STATE["coop"].
- Either file readable or writable by group or others is refused (an error on the card): friends.json
  holds friends' tokens, and share-tokens.json decides who reads this player.
- Tokens: python3 -I admin/modules/coop.py add|revoke|list [friend]. "add" prints the new token once.

config "life.coop": {"board": "Weekly quest board", "party_goal": 12}"""
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

if __name__ == "__main__":  # run as a script: make the "modules" package importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.common import STATE, ago, e, row  # noqa: E402

NAME, TITLE, TAB = "coop", "Co-op", "home"
SAMPLE_EVERY = 600
CONF = os.path.expanduser("~/.config/homelab-admin")
TOKENS_PATH = os.path.join(CONF, "share-tokens.json")
FRIENDS_PATH = os.path.join(CONF, "friends.json")
TIMEOUT = 5
MAX_BYTES = 16 * 1024
MAX_FRIENDS = 12
FAIL_LIMIT, FAIL_WINDOW = 100, 600  # bad tokens from everyone together: the backstop
CLIENT_FAIL_LIMIT = 5                # bad tokens from one client in FAIL_WINDOW
SHARE_PATH = "/share/v1/week"
TEXT_MAX, QUESTS_MAX, INT_MAX = 40, 8, 1000
_FRIEND = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
_HEX = re.compile(r"[0-9a-f]{64}")
_NETLOC = re.compile(r"(?P<host>[a-z0-9-]+(\.[a-z0-9-]+)*\.ts\.net)(:(?P<port>[0-9]{1,5}))?")
_WEEK = re.compile(r"\d{4}-W\d{2}")
_fails = collections.deque()  # monotonic times of bad tokens, newest last
_client_fails = {}  # client key -> deque of monotonic times of its bad tokens
_lock = threading.Lock()
_log = {"read": None, "errors": {}, "unsafe": {}}  # unsafe: file name -> error, when its mode is too open


# --- Strict schema for what a friend sends (and what we send). ---
def _text(v):
    """A short plain string: no control or markup characters, at most TEXT_MAX; None if not a string."""
    if not isinstance(v, str):
        return None
    v = "".join(ch for ch in v if ch not in "<>&\"'`" and unicodedata.category(ch)[0] != "C")
    return " ".join(v.split())[:TEXT_MAX]


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= INT_MAX else None


def _quest(q):
    if not isinstance(q, dict):
        return None
    out = {"name": _text(q.get("name")), "have": _int(q.get("have")), "need": _int(q.get("need")), "done": q.get("done")}
    if not out["name"] or out["have"] is None or out["need"] is None or not isinstance(out["done"], bool):
        return None
    return out


def _boss(b):
    if not isinstance(b, dict):
        return None
    out = {"name": _text(b.get("name")), "hp": _int(b.get("hp")), "max": _int(b.get("max")), "defeated": b.get("defeated")}
    if not out["name"] or out["hp"] is None or out["max"] is None or not isinstance(out["defeated"], bool):
        return None
    return out


def clean(d):
    """{v, name, level, title, week, quests, boss} with only those keys and checked types, or None.
    Bad quests are dropped; a bad name, level, title or week rejects the whole answer."""
    if not isinstance(d, dict) or d.get("v") != 1:
        return None
    name, title, level, week = _text(d.get("name")), _text(d.get("title")), _int(d.get("level")), d.get("week")
    if not name or title is None or level is None or not (isinstance(week, str) and _WEEK.fullmatch(week)):
        return None
    quests = d.get("quests") if isinstance(d.get("quests"), list) else []
    return {"v": 1, "name": name, "level": level, "title": title, "week": week,
            "quests": [q for q in map(_quest, quests[:QUESTS_MAX]) if q], "boss": _boss(d.get("boss"))}


def _week(today=None):
    y, w, _ = (today or date.today()).isocalendar()
    return f"{y}-W{w:02d}"


def _n(v):
    try:
        return max(0, min(INT_MAX, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


def summary(cfg):
    """What a friend sees of this player this week; nothing outside clean()'s keys."""
    from modules import life
    S = life.game(cfg)
    boss = S.get("boss") if isinstance(S.get("boss"), dict) else {}
    raw = {"v": 1, "name": S.get("name") or (cfg.get("life") or {}).get("name") or "Player",
           "level": _n(S.get("level")), "title": str(S.get("title") or ""), "week": _week(),
           "quests": [{"name": str(q.get("name") or ""), "have": _n(q.get("have")), "need": _n(q.get("need")),
                       "done": bool(q.get("done"))} for q in S.get("quests") or [] if isinstance(q, dict)],
           "boss": {"name": str(boss.get("name") or ""), "hp": _n(boss.get("hp")), "max": _n(boss.get("max")),
                    "defeated": bool(boss.get("defeated"))} if boss else None}
    return clean(raw) or {**raw, "name": "Player", "title": "", "quests": [], "boss": None}


# --- Sharing: the ShareHandler in app.py calls serve(). ---
def _read_json(path):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write_private(path, d):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # a .tmp left by a crash keeps its old mode otherwise
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, path)


def _private(path):
    """True when the file is missing or only its owner can read and write it; otherwise the error goes on
    the card and the caller doesn't use the file."""
    name = os.path.basename(path)
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        mode = 0
    except OSError as ex:
        _log["unsafe"][name] = f"{name}: {type(ex).__name__}"
        return False
    if mode & 0o077:
        _log["unsafe"][name] = f"{name} can be read or written by others (mode {mode & 0o777:o}): not used. chmod 600 it."
        return False
    _log["unsafe"].pop(name, None)
    return True


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _known(token):
    """True when the token's hash matches one in share-tokens.json; compares against every hash."""
    if not token or len(token) > 200:
        return False
    sent, ok = _hash(token), False
    for h in _read_json(TOKENS_PATH).values():
        if isinstance(h, str) and _HEX.fullmatch(h) and hmac.compare_digest(sent, h):
            ok = True
    return ok


def _reply(status, d):
    return status, json.dumps(d).encode()


def serve(cfg, auth, client=""):
    """(status, JSON body) for GET /share/v1/week with this Authorization header. client keys the
    per-client limit (app.py passes the Tailscale-User-Login header, else the peer address)."""
    now, client = time.monotonic(), str(client or "")[:200]
    with _lock:
        while _fails and now - _fails[0] > FAIL_WINDOW:
            _fails.popleft()
        for k in list(_client_fails):
            q = _client_fails[k]
            while q and now - q[0] > FAIL_WINDOW:
                q.popleft()
            if not q:
                del _client_fails[k]
        if len(_client_fails.get(client, ())) >= CLIENT_FAIL_LIMIT or len(_fails) >= FAIL_LIMIT:
            return _reply(429, {"error": "Too many attempts. Try later."})
        if not _private(TOKENS_PATH):
            print("coop: share-tokens.json is open to others, not serving", flush=True)
            return _reply(503, {"error": "Not available right now."})
        scheme, _, token = str(auth or "").strip().partition(" ")
        if scheme.lower() != "bearer" or not _known(token.strip()):
            _fails.append(now)
            _client_fails.setdefault(client, collections.deque()).append(now)
            return _reply(401, {"error": "Unknown share token."})
    try:
        return _reply(200, summary(cfg))
    except Exception as ex:
        print(f"coop: summary failed: {type(ex).__name__}", flush=True)
        return _reply(503, {"error": "Not available right now."})


# --- Reading friends. ---
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could point the request (and the friend's token) at 127.0.0.1 or anywhere: refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are refused", headers, fp)


def _opener():
    # No proxies from the environment, no redirects, and never remote.py's hub token.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def friend_url(url):
    """The friend's share URL, or ValueError: https and a netloc that is exactly <labels>.ts.net with an
    optional port 1-65535. Anything else in it (user info, backslashes, odd characters) is refused."""
    raw = str(url or "")
    u = urllib.parse.urlsplit(raw)
    m = _NETLOC.fullmatch(u.netloc.lower())
    port = m and m["port"]
    if (u.scheme != "https" or not m or "\\" in raw or any(ord(ch) < 33 or ord(ch) > 126 for ch in raw)
            or (port is not None and not 1 <= int(port) <= 65535)):
        raise ValueError("a friend's URL must be https://<host>.ts.net[:port]")
    return urllib.parse.urlunsplit(("https", m["host"] + (f":{int(port)}" if port else ""), SHARE_PATH, "", ""))


def fetch(url, token):
    """A friend's cleaned summary, or ValueError / OSError."""
    if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{1,200}", token):
        raise ValueError("the friend's token is malformed")
    req = urllib.request.Request(friend_url(url), headers={"Authorization": f"Bearer {token}",
                                                           "Accept": "application/json", "User-Agent": "homelab-coop/1"})
    with _opener().open(req, timeout=TIMEOUT) as r:
        if getattr(r, "status", 200) != 200:
            raise ValueError(f"HTTP {r.status}")
        raw = r.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("the answer is too big")
    d = clean(json.loads(raw))
    if d is None:
        raise ValueError("the answer doesn't match the schema")
    return d


def _row(name, d, me=False):
    boss = d.get("boss") or {}
    return {"name": name, "level": d["level"], "title": d["title"], "done": sum(q["done"] for q in d["quests"]),
            "need": len(d["quests"]), "boss_defeated": bool(boss.get("defeated")), "me": me}


def sample(cfg):
    friends = _read_json(FRIENDS_PATH) if _private(FRIENDS_PATH) else {}  # friends' tokens: owner only
    if not friends:
        STATE.pop("coop", None)
        return
    rows, errors = [], {}
    for label, f in list(friends.items())[:MAX_FRIENDS]:
        name = _text(label) or "Friend"
        try:
            if not isinstance(f, dict):
                raise ValueError("bad entry in friends.json")
            rows.append(_row(name, fetch(f.get("url"), f.get("token"))))
        except Exception as ex:  # one friend offline or odd never stops the board
            errors[name] = f"{type(ex).__name__}: {str(ex)[:120]}"
    me = summary(cfg)
    rows.insert(0, _row(me["name"], me, me=True))
    conf = (cfg.get("life") or {}).get("coop") or {}
    STATE["coop"] = {"at": time.time(), "board": _text(conf.get("board")) or "Weekly quest board",
                     "goal": _n(conf.get("party_goal", 12)), "total": sum(r["done"] for r in rows), "rows": rows}
    _log.update(read=time.time(), errors=errors)


def card(cfg):
    shared = sorted(_read_json(TOKENS_PATH))
    friends = sorted(_read_json(FRIENDS_PATH))
    board = STATE.get("coop")
    out = [row("Sharing with", e(", ".join(shared) if shared else "nobody")),
           row("Reading", e(", ".join(friends) if friends else "no friends yet"))]
    if board:
        out.append(row(board["board"], e(f"{board['total']} / {board['goal']} quests done together")))
    if _log["read"]:
        out.append(row("Last read", e(f"{ago(time.time() - _log['read'])} ago")))
    for err in list(_log["unsafe"].values()):
        out.append(f'<div class="alert bad">{e(err)}</div>')
    for name, err in _log["errors"].items():
        out.append(f'<div class="alert bad">{e(name)}: {e(err)}</div>')
    if not shared and not friends:
        out.append('<small class="muted">Off. Set it up with python3 -I admin/modules/coop.py add &lt;friend&gt;.</small>')
    return "".join(out)


# --- CLI: share tokens. ---
def main(argv):
    cmd, friend = (argv + [None, None])[:2]
    tokens = _read_json(TOKENS_PATH)
    if cmd == "list" and friend is None:
        print("\n".join(sorted(tokens)) or "No share tokens.")
        return 0
    if cmd in ("add", "revoke") and friend and _FRIEND.fullmatch(friend):
        if cmd == "revoke":
            if tokens.pop(friend, None) is None:
                print(f"No share token for {friend}.")
                return 1
            _write_private(TOKENS_PATH, tokens)
            print(f"Revoked {friend}'s share token.")
            return 0
        token = secrets.token_urlsafe(32)
        tokens[friend] = _hash(token)
        _write_private(TOKENS_PATH, tokens)
        print(f"Share token for {friend} (shown once; it replaces any earlier one):\n{token}")
        return 0
    print("usage: coop.py add <friend> | revoke <friend> | list\n"
          "friend: letters, digits, space, . _ - (up to 40)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
