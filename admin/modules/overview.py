"""Home tab: the numbers that matter at a glance, and agents waiting on you. tiles(): the Desktop tile."""
from modules import agents, machine
from modules.common import STATE, ago, e, level

NAME, TITLE, TAB = "overview", "At a glance", "home"


def _tile(label, value, lvl="", sub="", tint=None):
    """A stat tile: grey when calm, tinted (tint-<level>) when it needs a look; warn and bad tint by default."""
    tint = tint if tint is not None else lvl if lvl in ("warn", "bad") else ""
    return (f'<div class="tile{f" tint-{tint}" if tint else ""}"><small>{e(label)}</small><b class="{lvl}">{e(value)}</b>'
            f'{f"<small>{e(sub)}</small>" if sub else ""}</div>')


def tiles(cfg):
    """Home tile (app.py): the desktop's wall power and CPU temperature; blue while it's awake, warn or bad when the
    PSU load or the CPU is high. A tap opens the Machine tab. Agents have their own tile (modules/agents.py)."""
    m = STATE.get("machine")
    if not m:
        return []
    cpu_t = (m.get("temps") or {}).get("cpu")
    w, c = machine.PSU_LOAD_LIMITS
    load = m.get("psu_load", 0)
    lvls = ("bad" if load >= c else "warn" if load >= w else "ok", level(cpu_t, *machine.LIMITS["cpu"]))
    tint = "bad" if "bad" in lvls else "warn" if "warn" in lvls else "busy"
    value = f"{m.get('wall', 0):.0f} W" + (f" · {cpu_t:.0f}°" if cpu_t is not None else "")
    return [{"key": "desktop", "label": "Desktop", "value": value, "tint": tint, "icon": "monitor",
             "open": {"tab": "machine"}, "span": 1, "order": 70}]


def card(cfg):
    m, a = STATE.get("machine") or {}, STATE.get("agents") or {}
    items = a.get("items", [])
    counts = {k: sum(x["status"] == k for x in items) for k in agents.STATUS}
    g = m.get("gpu") or {}
    cpu_t, gpu_t = (m.get("temps") or {}).get("cpu"), g.get("temp")
    alerts = len(m.get("failed", [])) + len(m.get("events", []))
    w, c = machine.PSU_LOAD_LIMITS
    load = m.get("psu_load", 0)
    tiles = [
        _tile("Desktop", "awake", "ok", f"up {ago(m['uptime'])}" if m else "", tint="busy"),
        _tile("Power", f"{m.get('wall', 0):.0f} W", "bad" if load >= c else "warn" if load >= w else "ok",
              f"{load * 100:.0f}% of PSU"),
        _tile("CPU", f"{cpu_t:.0f}°" if cpu_t is not None else "n/a", level(cpu_t, *machine.LIMITS["cpu"]),
              f"{m.get('cpu_util', 0):.0f}% busy"),
        _tile("GPU", f"{gpu_t:.0f}°" if gpu_t is not None else "n/a", level(gpu_t, *machine.LIMITS["gpu"]),
              f"{g.get('util', 0):.0f}% · {g.get('power', 0):.0f} W"),
        _tile("Agents", f"{counts['working'] + counts['stuck']} working", "busy" if counts["working"] + counts["stuck"] else "dim",
              f"{counts['waiting']} need you · {counts['atpc']} at PC · {counts['idle']} idle",
              tint="need" if counts["waiting"] else "busy" if counts["working"] + counts["stuck"] else ""),
        _tile("Alerts", str(alerts) if alerts else "none", "bad" if alerts else "ok", "since boot"),
    ]
    out = [f'<div class="tiles">{"".join(tiles)}</div>']
    waiting = [x for x in items if x["status"] == "waiting"]
    working = [x for x in items if x["status"] in ("working", "stuck")]  # a stuck agent is still on it
    lists = []  # side by side on a landscape screen
    lv = agents.levels() if waiting or working else {}
    if waiting:
        lists.append(f"<div><h3>Needs you ({len(waiting)})</h3>" + "".join(agents.item_row(x, lv) for x in waiting) + "</div>")
    if working:
        lists.append(f"<div><h3>Working now ({len(working)})</h3>" + "".join(agents.item_row(x, lv) for x in working) + "</div>")
    if lists:
        out.append(f'<div class="lists">{"".join(lists)}</div>')
    elif items:
        out.append('<p class="muted">All agents are idle.</p>')
    return "".join(out)
