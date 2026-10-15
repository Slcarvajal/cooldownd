"""Coding agents (Claude Code, Codex, …) in Orca terminals, grouped by machine and project.
Tap one to see its screen, answer it, or interrupt it."""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from modules.common import STATE, ago, e, orca, orca_send

NAME, TITLE, TAB = "agents", "Agents", "agents"
SAMPLE_EVERY = 6
WORKING_WINDOW = 8  # output within this many seconds counts as working
QUESTION = re.compile(r"do you want|would you like|should i\b|shall i\b|want me to|❯\s*1\.|\(y/n\)|"
                      r"need (two|an?|your) (answers?|decision)|which (one|option)|\?\s*$", re.I)
# Claude Code's live spinner line, e.g. "· Twisting… (7s · thinking with high effort)"
SPINNER = re.compile(r"^\s*\S\s+[A-Z][a-z]+…\s*\(\d", re.M)
KEYS = {"esc": ("\x1b", False), "ctrl-c": ("\x03", False), "enter": ("", True),
        "1": ("1", True), "2": ("2", True), "3": ("3", True), "up": ("\x1b[A", False), "down": ("\x1b[B", False),
        "yes": ("Yes, go ahead.", True), "no": ("No, don't do that.", True)}
STATUS = {"waiting": ("warn", "needs you"), "atpc": ("dim", "at PC"), "working": ("busy", "working"), "idle": ("dim", "idle")}
# Questions set aside with "PC work": handle|summary keys. They stop paging until the question changes.
DEFERRED = os.path.expanduser("~/.local/share/homelab-admin/deferred.json")
QUICK = [("pc", "💻 PC work"), ("yes", "✅ Go ahead"), ("no", "✋ No"), ("1", "1"), ("2", "2"), ("3", "3")]


def _deferred():
    try:
        return set(json.load(open(DEFERRED)))
    except (OSError, ValueError):
        return set()


def key_of(a):
    return f'{a["handle"]}|{a["summary"]}'


def _screen(handle, limit=None):
    r = orca("terminal", "read", "--terminal", handle, "--screen")
    lines = ((r or {}).get("terminal") or {}).get("tail") or []
    return lines[-limit:] if limit else lines


def _content(lines):
    """Lines above Claude Code's input box (the last two ──── rules), trailing blanks dropped."""
    rules = [i for i, l in enumerate(lines) if l.startswith("────")]
    body = lines[:rules[-2]] if len(rules) >= 2 else lines
    while body and not body[-1].strip():
        body = body[:-1]
    return body


BACKGROUND = re.compile(r"Waiting for \d+ background agents?", re.I)
DONE = re.compile(r"^\W*[A-Z][a-zé]+ed for \d")  # "✻ Sautéed for 1m 41s · done …"


def _meaningful(line):
    """Real text, not a divider, banner art or a turn-timing line."""
    s = line.strip()
    letters = sum(ch.isalpha() for ch in s)
    return letters >= 4 and letters >= 0.4 * len(s) and not DONE.match(s)


def _summary(body):
    """The ※ recap Claude Code prints after a turn, else the last line of output."""
    for i in range(len(body) - 1, -1, -1):
        if "※ recap:" in body[i]:
            text = [body[i].split("recap:", 1)[1].strip()]
            text += [l.strip() for l in body[i + 1:] if l.startswith("  ")]
            return re.sub(r"\s*\(disable recaps in /config\)", "", " ".join(text))
    for line in reversed(body):
        if _meaningful(line):
            return line.strip().lstrip("⎿●✻✽✶✢·* ").strip()
    return ""


def _status(title, body, age):
    tail = "\n".join(body[-20:])
    if ("esc to interrupt" in tail.lower() or SPINNER.search(tail) or BACKGROUND.search(tail)
            or (title and ("⠀" <= title[0] <= "⣿" or title[0] in "◐◑◒◓"))):
        return "working"  # Claude Code animates a braille or quarter-circle spinner in the title while busy
    if QUESTION.search("\n".join(body[-8:])):
        return "waiting"
    return "working" if age < WORKING_WINDOW else "idle"


def sample(cfg):
    listing = orca("terminal", "list")
    if listing is None:
        STATE["agents"] = {"at": time.time(), "error": "Orca isn't running on this machine.", "items": []}
        return
    hosts = {h["id"]: h.get("machineName") or h.get("name") for h in (orca("host", "list") or {}).get("hosts", [])}
    terms = [t for t in listing.get("terminals", []) if t.get("agentIdentity")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        screens = list(pool.map(lambda t: _screen(t["handle"]), terms))
    now, items = time.time(), []
    for t, lines in zip(terms, screens):
        body = _content(lines)
        age = now - (t.get("lastOutputAt") or 0) / 1000
        title = (t.get("title") or "").strip()
        items.append({
            "handle": t["handle"], "host": hosts.get(t.get("executionHostId"), t.get("executionHostId") or "?"),
            "project": t.get("worktreePath", "").rstrip("/").rsplit("/", 1)[-1],
            "path": t.get("worktreePath", ""), "branch": (t.get("branch") or "").removeprefix("refs/heads/"),
            "agent": t["agentIdentity"], "title": re.sub(r"^[\u2800-\u28ff✳\s]+", "", title) or t["agentIdentity"],
            "status": _status(title, body, age), "summary": _summary(body)[:220], "age": age,
            "writable": t.get("writable", False),
        })
    deferred = _deferred()
    for a in items:
        if a["status"] == "waiting" and key_of(a) in deferred:
            a["status"] = "atpc"
    order = {"waiting": 0, "atpc": 1, "working": 2, "idle": 3}
    items.sort(key=lambda a: (a["host"], a["project"], order[a["status"]], a["age"]))
    STATE["agents"] = {"at": now, "items": items}


def item_row(a):
    lvl, label = STATUS[a["status"]]
    return (f'<button class="agent" data-sheet="agents/view/{e(a["handle"])}">'
            f'<i class="dot {lvl}"></i><span class="who"><b>{e(a["title"])}</b>'
            f'<small>{e(a["summary"] or a["agent"])}</small></span>'
            f'<span class="when"><span class="pill {lvl}">{label}</span><small>{ago(a["age"])}</small></span></button>')


def card(cfg):
    s = STATE.get("agents")
    if not s:
        sample(cfg)
        s = STATE["agents"]
    if s.get("error"):
        return f'<p class="muted">{e(s["error"])}</p>'
    if not s["items"]:
        return '<p class="muted">No agents running.</p>'
    counts = {k: sum(a["status"] == k for a in s["items"]) for k in STATUS}
    out = ['<div class="counts">' + "".join(
        f'<span class="pill {STATUS[k][0]}">{counts[k]} {STATUS[k][1]}</span>' for k in STATUS) + "</div>"]
    host = project = None
    for a in s["items"]:
        if a["host"] != host:
            host, project = a["host"], None
            out.append(f'<h3 class="host">🖥 {e(host)}</h3>')
        if a["project"] != project:
            project = a["project"]
            out.append(f'<div class="project">{e(project)}<small>{e(a["branch"])}</small></div>')
        out.append(item_row(a))
    return "".join(out)


def _find(handle):
    for a in (STATE.get("agents") or {}).get("items", []):
        if a["handle"] == handle:
            return a
    raise ValueError("That agent isn't running anymore.")


LIST_ITEM = re.compile(r"^\s*([-*•●⎿❯>]|\d+[.)]|[A-Z][\w ]{0,30}:\s)")


def _reflow(lines):
    """Rejoin lines the agent hard-wrapped at the desktop terminal's width, so text reflows on a phone.
    A line continues the previous one only if that one ran long (it hit the wrap) and the indent
    matches: the same indent for a paragraph, deeper for a list item's hanging indent."""
    out, base, is_item, last_len = [], 0, False, 0
    for line in lines:
        indent = len(line) - len(line.lstrip())
        cont = (out and line.strip() and last_len >= 45 and not LIST_ITEM.match(line)
                and (indent > base if is_item else indent == base))
        if cont:
            out[-1] = out[-1].rstrip() + " " + line.strip()
        else:
            out.append(line)
            base, is_item = indent, bool(LIST_ITEM.match(line))
        last_len = len(line.strip())
    return out


def view(cfg, handle):
    a = _find(handle)
    body = _reflow(_content(_screen(handle)))[-50:]
    lvl, label = STATUS[a["status"]]
    keys = "".join(f'<button class="key" data-post="agents/send/{e(handle)}" data-key="{k}">{k}</button>'
                   for k in ("esc", "enter", "1", "2", "3", "ctrl-c"))
    quick = "".join(
        f'<button class="key" data-post="agents/defer/{e(handle)}" data-key="pc">{label}</button>' if k == "pc" else
        f'<button class="key" data-post="agents/send/{e(handle)}" data-key="{k}">{label}</button>' for k, label in QUICK)
    return (f'<div class="sheet-meta"><span class="pill {lvl}">{label}</span> {e(a["agent"])} · {e(a["host"])} · '
            f'{e(a["project"])} <small>{e(a["branch"])}</small></div>'
            f'<div class="quick">{quick}</div>'
            f'<pre class="screen">{e(chr(10).join(body))}</pre>'
            f'<form data-post="agents/send/{e(handle)}"><textarea name="text" rows="3" '
            f'placeholder="Reply to {e(a["title"])}…"></textarea><button>Send</button></form>'
            f'<div class="keys">{keys}</div>')


_recent = {}  # (handle, what) -> time sent; the same answer twice within 15 s is a double tap


def send(cfg, handle, body):
    a = _find(handle)
    if not a["writable"]:
        return "That terminal is read-only."
    if a["status"] == "working" and body.get("key") not in ("esc", "ctrl-c"):  # stopping a busy agent stays possible
        return f"{a['title']} is busy right now. Nothing was sent."
    key = body.get("key")
    if key:
        if key not in KEYS:
            raise ValueError(f"unknown key {key!r}")
        text, enter = KEYS[key]
    else:
        text, enter = (body.get("text") or "").strip(), True
        if not text:
            return "Nothing to send."
    what = key or text
    now = time.time()
    if now - _recent.get((handle, what), 0) < 15:
        return f"Already sent to {a['title']} a moment ago. Nothing sent again."
    _recent[(handle, what)] = now
    state, detail = orca_send(handle, text, enter)
    print(f"agents.send {a['host']}/{a['project']}/{a['title']!r}: {what[:200]!r} -> {state} {detail}", flush=True)
    if state == "refused":
        _recent.pop((handle, what), None)
        raise RuntimeError(f"Not sent: {detail}.")
    if state == "unknown":
        return f"{detail}. It may already be in {a['title']}; check its screen before sending again."
    if state == "typed":
        return f"Sent to {a['title']}, but {detail}."
    return f"Sent to {a['title']}." if not key else f"Pressed {key} in {a['title']}."


def defer(cfg, handle, body):
    """PC work: send nothing; stop paging this question until it changes."""
    a = _find(handle)
    waiting = {key_of(x) for x in (STATE.get("agents") or {}).get("items", []) if x["status"] in ("waiting", "atpc")}
    keep = (_deferred() & waiting) | {key_of(a)}  # drop keys of questions that are gone
    os.makedirs(os.path.dirname(DEFERRED), exist_ok=True)
    with open(DEFERRED + ".tmp", "w") as f:
        json.dump(sorted(keep), f)
    os.replace(DEFERRED + ".tmp", DEFERRED)
    a["status"] = "atpc"
    print(f"agents.defer {a['host']}/{a['project']}/{a['title']!r}", flush=True)
    return f"{a['title']} is saved for the PC."


ACTIONS = {"send": send, "defer": defer}
SHEETS = {"view": view}
