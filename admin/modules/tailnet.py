"""Home lab devices first; everything else on the tailnet folded away."""
from datetime import datetime, timezone

from modules.common import e, name, pill, row, tailnet

NAME, TITLE, TAB = "tailnet", "Devices", "machine"


def _seen(node):
    try:
        ago = datetime.now(timezone.utc) - datetime.fromisoformat(node["LastSeen"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return "offline"
    mins = int(ago.total_seconds() // 60)
    return f"seen {mins}m ago" if mins < 60 else f"seen {mins // 60}h ago" if mins < 2880 else f"seen {mins // 1440}d ago"


def _line(node, is_self=False):
    online = is_self or node.get("Online", False)
    ip = (node.get("TailscaleIPs") or [""])[0]
    label = f"{name(node)} · {node.get('OS', '')}"
    return row(label, f'<small>{e(ip)}</small> {pill(online, "online" if online else _seen(node))}')


def card(cfg):
    ts = tailnet()
    me = ts.get("Self") or {}
    # Funnel's ingress relays show up as peers; they aren't devices
    nodes = [n for n in (ts.get("Peer") or {}).values() if n.get("HostName") != "funnel-ingress-node"]
    lab = {h.lower() for h in cfg.get("homelab_devices", [])}
    mine = [_line(me, True)] + [_line(n) for n in nodes if name(n) in lab]
    other = [_line(n) for n in nodes if name(n) not in lab]
    out = "".join(mine)
    if other:
        out += f'<details><summary>Other devices on the tailnet ({len(other)})</summary>{"".join(other)}</details>'
    return out
