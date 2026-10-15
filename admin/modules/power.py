"""Sleep this machine (or, through its remote panel, another one); wake the others with Wake-on-LAN.
tiles(): a Wake tile on the Home grid for each other machine while it's asleep."""
import socket
import subprocess
import threading
import time

from modules.common import e, peer, pill, self_host

NAME, TITLE, TAB = "power", "Power", "machine"


def magic_packet(mac):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"bad MAC {mac!r}")
    return b"\xff" * 6 + raw * 16


def send_wol(mac, broadcast):
    pkt = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in dict.fromkeys([broadcast, "255.255.255.255"]):
            s.sendto(pkt, (addr, 9))


def _machine(cfg, mid):
    for m in cfg["machines"]:
        if m["id"] == mid:
            return m
    raise ValueError(f"unknown machine {mid!r}")


def card(cfg):
    me = self_host()
    out = []
    for m in cfg["machines"]:
        node = peer(m["tailnet"]) or {}
        is_self = m["tailnet"].lower() == me
        online = is_self or node.get("Online", False)
        if is_self:
            btn = (f'<button data-action="power/sleep/{e(m["id"])}" '
                   f'data-confirm="Put {e(m["name"])} to sleep?">Sleep</button>')
            # the machine can't wake itself; point at the panel that can
            link = f'<a class="open" href="{e(m["wake_from"])}">Wake it from the Pi panel</a>' if m.get("wake_from") else ""
        else:
            btn = f'<button data-action="power/wake/{e(m["id"])}">Wake</button>'
            if online and m.get("remote"):
                # awake: sleep it through its own panel (remote.py forwards "<remote>.power" actions)
                btn += (f' <button data-action="{e(m["remote"])}.power/sleep/{e(m["id"])}" '
                        f'data-confirm="Put {e(m["name"])} to sleep?">Sleep</button>')
            link = ""
        out.append(
            f'<div class="machine"><div class="row"><strong>{e(m["name"])}</strong>'
            f'{pill(online, "awake" if online else "asleep / off")}</div>{btn}{link}</div>'
        )
    return "".join(out)


def tiles(cfg):
    """Home tiles (app.py): each other machine while it's asleep, which a tap wakes. Awake, the desktop has its
    own tile (modules/overview.py), and sleeping it stays on the card, behind its confirmation."""
    me, out = self_host(), []
    if not me:  # no tailnet status: who's asleep is unknown (this could even be the machine itself)
        return []
    for m in cfg.get("machines", []):
        if m["tailnet"].lower() == me or (peer(m["tailnet"]) or {}).get("Online", False):
            continue
        out.append({"key": f"wake-{m['id']}", "label": m["name"].split(" · ")[0], "value": "Asleep · tap to wake",
                    "tint": None, "icon": "power", "open": {"action": f"power/wake/{m['id']}"}, "span": 1, "order": 70})
    return out


def _sleep_soon():
    time.sleep(2)  # let the response reach the phone first
    subprocess.run(["systemctl", "suspend"], check=False)


def sleep(cfg, mid, body):
    m = _machine(cfg, mid)
    if m["tailnet"].lower() != self_host():
        return "Sleep only works on the machine running this panel."
    threading.Thread(target=_sleep_soon, daemon=True).start()
    return f"{m['name']} is going to sleep."


def wake(cfg, mid, body):
    m = _machine(cfg, mid)
    if m["tailnet"].lower() == self_host():
        return "This machine is already awake."
    send_wol(m["mac"], m["broadcast"])
    return f"Wake signal sent to {m['name']}. Give it ~20 seconds."


ACTIONS = {"sleep": sleep, "wake": wake}
