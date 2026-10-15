"""One-tap commands from config.json. Only listed commands can run; there's no free shell."""
import subprocess

from modules.common import e

NAME, TITLE, TAB = "commands", "Commands", "commands"


def card(cfg):
    out = []
    for c in cfg.get("commands", []):
        confirm = f' data-confirm="Run “{e(c["label"])}”?"' if c.get("confirm") else ""
        out.append(f'<button class="cmd" data-action="commands/run/{e(c["id"])}"{confirm}>'
                   f'<b>{e(c["label"])}</b><small>{e(c.get("about", ""))}</small></button>')
    return "".join(out) or '<p class="muted">No commands configured.</p>'


def run_command(cfg, cid, body):
    c = next((c for c in cfg.get("commands", []) if c["id"] == cid), None)
    if c is None:
        raise ValueError(f"unknown command {cid!r}")
    print(f"commands.run {cid}", flush=True)
    try:
        r = subprocess.run(c["cmd"], capture_output=True, text=True, timeout=c.get("timeout", 30),
                           stdin=subprocess.DEVNULL, shell=isinstance(c["cmd"], str))
        output, code = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip(), r.returncode
    except subprocess.TimeoutExpired:
        output, code = f"Timed out after {c.get('timeout', 30)} s.", -1
    return {"message": f"{c['label']}: {'done' if code == 0 else f'exit {code}'}",
            "title": c["label"], "output": output[-8000:] or "(no output)"}


ACTIONS = {"run": run_command}
