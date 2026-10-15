"""Helpers shared by modules."""
import hmac
import html
import json
import os
import subprocess
import time

# Latest background samples, written by each module's sample() and read by any card.
STATE = {}
# Orca IDE's CLI. /usr/bin/orca is GNOME's screen reader, not this.
ORCA = os.path.expanduser("~/.local/bin/orca-ide")
# Shared secret a tagged panel (the Switch) sends to the panels it reads (modules/remote.py). Never in the repo.
TOKEN_PATH = os.path.expanduser("~/.config/homelab-admin/hub-token")

_cache = {}


def hub_token():
    try:
        return open(TOKEN_PATH).read().strip()
    except OSError:
        return ""


def token_ok(sent):
    want = hub_token()
    return bool(want and sent) and hmac.compare_digest(sent.encode(), want.encode())


def run(*cmd, timeout=5):
    """stdout of a command, or "" if it fails or is missing."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def orca(*args, timeout=10):
    """`orca-ide <args> --json` result, or None when Orca isn't running or the call fails."""
    try:
        d = json.loads(run(ORCA, *args, "--json", timeout=timeout) or "{}")
    except ValueError:
        return None
    return d.get("result") if d.get("ok") else None


def orca_send(handle, text, enter):
    """Type into an Orca terminal and say what actually happened, so a caller never retries text that
    already arrived. Returns (state, detail): "sent" (turn started or plain terminal), "typed" (accepted,
    but the agent hasn't started a turn), "unknown" (no clear answer: the text may be there),
    or "refused" (Orca said no)."""
    try:
        r = subprocess.run([ORCA, "terminal", "send", "--terminal", handle, "--text", text,
                            *(["--enter", "--wait-submit", "4"] if enter else []), "--json"],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        d = json.loads(r.stdout or "{}")
    except subprocess.TimeoutExpired:
        return "unknown", "Orca didn't answer in 30 s"
    except (OSError, ValueError):
        return "unknown", "Orca's reply couldn't be read"
    if not d.get("ok"):
        err = d.get("error") or {}
        return "refused", str(err.get("message") if isinstance(err, dict) else err or "Orca refused the input")[:200]
    send = (d.get("result") or {}).get("send") or {}
    if not send.get("accepted"):
        return "refused", "Orca didn't accept the input"
    stages = (send.get("prompt") or {}).get("stages") or []
    if not enter or (send.get("prompt") or {}).get("observation") != "supported" or len(stages) > 1:
        return "sent", ""
    return "typed", "the agent hasn't started on it yet"


def tailnet():
    """`tailscale status --json`, cached for a few seconds."""
    hit = _cache.get("ts")
    if hit and time.monotonic() - hit[0] < 5:
        return hit[1]
    try:
        data = json.loads(run("tailscale", "status", "--json") or "{}")
    except ValueError:
        data = {}
    _cache["ts"] = (time.monotonic(), data)
    return data


def name(node):
    """Device name as the tailnet shows it (iPhones report HostName "localhost")."""
    return (node.get("DNSName") or "").split(".")[0].lower() or node.get("HostName", "").lower()


def self_host():
    return name(tailnet().get("Self") or {})


def peer(hostname):
    """Tailnet node by host name (self included), or None."""
    ts = tailnet()
    for node in [ts.get("Self") or {}, *(ts.get("Peer") or {}).values()]:
        if name(node) == hostname.lower():
            return node
    return None


def e(s):
    return html.escape(str(s))


def level(v, warn, crit):
    """CSS level for a value against (warn, crit) thresholds."""
    if v is None:
        return "dim"
    return "bad" if v >= crit else "warn" if v >= warn else "ok"


def pill(ok, text):
    """Status pill; `ok` is a bool or a level name (ok / warn / bad / dim / busy)."""
    lvl = ok if isinstance(ok, str) else "ok" if ok else "bad"
    return f'<span class="pill {lvl}">{e(text)}</span>'


def row(label, value):
    return f'<div class="row"><span>{e(label)}</span><span>{value}</span></div>'


def bar(frac, lvl="ok"):
    pct = max(0.0, min(1.0, frac or 0)) * 100
    return f'<div class="bar {lvl}"><i style="width:{pct:.0f}%"></i></div>'


def spark(values, lo, hi, cls="cyan"):
    """Inline SVG sparkline of recent samples."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return '<svg class="spark"></svg>'
    rng = max(hi - lo, 1e-6)
    step = 100 / (len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{24 - max(0, min(1, (v - lo) / rng)) * 22 - 1:.1f}" for i, v in enumerate(vals))
    return (f'<svg class="spark {cls}" viewBox="0 0 100 24" preserveAspectRatio="none">'
            f'<polyline points="{pts}" vector-effect="non-scaling-stroke"/></svg>')


def ago(seconds):
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"
