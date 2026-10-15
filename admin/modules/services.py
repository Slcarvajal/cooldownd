"""State of the services listed in config.json (start/stop for those marked "control"),
plus running Docker containers."""
import subprocess

from modules.common import e, pill, run

NAME, TITLE, TAB = "services", "Services", "machine"


def _ctl(s):
    return ["systemctl", "--user"] if s.get("user") else ["systemctl"]


def card(cfg):
    out = []
    for s in cfg["services"]:
        state = run(*_ctl(s), "is-active", s["unit"]) or "unknown"
        active = state == "active"
        btns = ""
        if s.get("control"):
            ops = ("restart", "stop") if active else ("start",)
            btns = "".join(f'<button class="mini" data-action="services/{op}/{e(s["unit"])}" '
                           f'data-confirm="{op.capitalize()} {e(s["label"])}?">{op}</button>' for op in ops)
        out.append(f'<div class="row"><span>{e(s["label"])}</span><span>{btns}{pill(active, state)}</span></div>')
    for c in [c for c in run("docker", "ps", "--format", "{{.Names}}\t{{.Status}}").splitlines() if c]:
        n, status = c.split("\t", 1)
        out.append(f'<div class="row"><span>🐳 {e(n)}</span><span>{pill(status.startswith("Up"), status)}</span></div>')
    return "".join(out)


def _op(op):
    def act(cfg, unit, body):
        s = next((s for s in cfg["services"] if s["unit"] == unit and s.get("control")), None)
        if s is None:
            raise ValueError(f"{unit} isn't controllable from the panel")
        r = subprocess.run([*_ctl(s), op, unit], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or f"systemctl {op} failed")
        print(f"services.{op} {unit}", flush=True)
        return f"{s['label']}: {op} done."
    return act


ACTIONS = {op: _op(op) for op in ("start", "stop", "restart")}
