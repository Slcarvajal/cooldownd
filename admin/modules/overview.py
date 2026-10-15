"""Home tab: the numbers that matter at a glance, and agents waiting on you."""
from modules import agents, machine
from modules.common import STATE, ago, e, level

NAME, TITLE, TAB = "overview", "At a glance", "home"


def _tile(label, value, lvl="", sub=""):
    return (f'<div class="tile"><small>{e(label)}</small><b class="{lvl}">{e(value)}</b>'
            f'{f"<small>{e(sub)}</small>" if sub else ""}</div>')


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
        _tile("Desktop", "awake", "ok", f"up {ago(m['uptime'])}" if m else ""),
        _tile("Power", f"{m.get('wall', 0):.0f} W", "bad" if load >= c else "warn" if load >= w else "ok",
              f"{load * 100:.0f}% of PSU"),
        _tile("CPU", f"{cpu_t:.0f}°" if cpu_t is not None else "n/a", level(cpu_t, *machine.LIMITS["cpu"]),
              f"{m.get('cpu_util', 0):.0f}% busy"),
        _tile("GPU", f"{gpu_t:.0f}°" if gpu_t is not None else "n/a", level(gpu_t, *machine.LIMITS["gpu"]),
              f"{g.get('util', 0):.0f}% · {g.get('power', 0):.0f} W"),
        _tile("Agents", f"{counts['working']} working", "busy" if counts["working"] else "dim",
              f"{counts['waiting']} need you · {counts['atpc']} at PC · {counts['idle']} idle"),
        _tile("Alerts", str(alerts) if alerts else "none", "bad" if alerts else "ok", "since boot"),
    ]
    out = [f'<div class="tiles">{"".join(tiles)}</div>']
    waiting = [x for x in items if x["status"] == "waiting"]
    working = [x for x in items if x["status"] == "working"]
    lists = []  # side by side on a landscape screen
    if waiting:
        lists.append(f"<div><h3>Needs you ({len(waiting)})</h3>" + "".join(agents.item_row(x) for x in waiting) + "</div>")
    if working:
        lists.append(f"<div><h3>Working now ({len(working)})</h3>" + "".join(agents.item_row(x) for x in working) + "</div>")
    if lists:
        out.append(f'<div class="lists">{"".join(lists)}</div>')
    elif items:
        out.append('<p class="muted">All agents are idle.</p>')
    return "".join(out)
