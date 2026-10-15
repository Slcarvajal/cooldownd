"""Temperatures, power draw, usage and hardware alerts for a Linux desktop with an NVIDIA GPU.
The power estimate is tuned for one build (Ryzen 7 5700X, RTX 5060 Ti): set your PSU's size in
config "psu_watts" (550 by default), and CPU_IDLE_W / CPU_MAX_W below for another CPU."""
import glob
import os
import re
import shutil
import subprocess
import time
from collections import deque

from modules.common import STATE, bar, e, level, pill, row, spark

NAME, TITLE, TAB = "machine", "Hardware", "machine"
SAMPLE_EVERY = 2

PSU_WATTS = 550
PSU_EFFICIENCY = 0.87          # typical 80+ Bronze/Gold at 20-50% load
CPU_IDLE_W, CPU_MAX_W = 12, 88  # 5700X: PPT limit 88 W
BASE_W = 35                     # board + RAM + NVMe + fans + USB, rough
LIMITS = {"cpu": (80, 90), "gpu": (80, 87), "nvme": (65, 75), "board": (70, 85)}  # (warn, crit) °C
PSU_LOAD_LIMITS = (0.70, 0.85)
THROTTLE_BITS = {
    0x08: ("HW slowdown", "bad"),
    0x20: ("SW thermal slowdown", "warn"),
    0x40: ("HW thermal slowdown", "bad"),
    0x80: ("HW power brake (PSU!)", "bad"),
}
CRIT_PATTERNS = re.compile(
    r"machine check|mce:|hardware error|kernel panic|NVRM: Xid|\bBUG:|soft lockup|"
    r"hard lockup|hung_task|blocked for more than|PCIe Bus Error.*Uncorrected|"
    r"I/O error|EXT4-fs error|BTRFS (error|critical)|Out of memory|oom-kill|"
    r"critical temperature|thermal shutdown|nvme.*(timeout|reset|controller is down)|"
    r"voltage|undervolt|watchdog: ", re.I)
WARN_PATTERNS = re.compile(
    r"PCIe Bus Error|AER:|thermal|throttl|segfault|ata\d.*(error|failed)|"
    r"usb.*(over-current|disabled)|nvidia.*(error|fail)|amdgpu.*error|"
    r"Corrected error|link is not ready|fan", re.I)
NOISE = re.compile(
    r"kauditd_printk_skb|hogged CPU for|No UUID available|unchecked data buffer|"
    r"journal.*corrupted or uncleanly|bluetoothd|Hands-Free|record_cb", re.I)

HISTORY = 90  # 3 minutes at one sample every 2 s
hist = {k: deque(maxlen=HISTORY) for k in ("cpu", "gpu", "watts")}
_prev = {"stat": None, "rapl": None}
_logs = {"t": 0.0, "events": [], "failed": []}


def _read(path, cast=str):
    try:
        with open(path) as f:
            return cast(f.read().strip())
    except (OSError, ValueError):
        return None


def _hwmon(name):
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(f"{d}/name") == name:
            return d
    return None


def _temps():
    out = {}
    if k10 := _hwmon("k10temp"):
        out["cpu"] = (_read(f"{k10}/temp1_input", int) or 0) / 1000
    if nv := _hwmon("nvme"):
        out["nvme"] = (_read(f"{nv}/temp1_input", int) or 0) / 1000
    if gb := _hwmon("gigabyte_wmi"):
        vals = [v / 1000 for f in sorted(glob.glob(f"{gb}/temp*_input"))
                if not f.endswith("temp3_input")  # CPU socket, already shown as CPU
                and (v := _read(f, int)) and 5000 < v < 120000]
        out["board"] = max(vals) if vals else None
    return out


def _gpu():
    if not shutil.which("nvidia-smi"):
        return None
    q = ("name,temperature.gpu,power.draw,power.limit,fan.speed,utilization.gpu,"
         "memory.used,memory.total,clocks.gr,clocks_event_reasons.active")
    try:
        r = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=3)
        p = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None

    def num(s):
        return float(s) if re.match(r"^[\d.]+$", s) else 0.0
    return {"name": p[0].replace("NVIDIA GeForce ", ""), "temp": num(p[1]), "power": num(p[2]),
            "limit": num(p[3]), "fan": num(p[4]), "util": num(p[5]), "mem_used": num(p[6]),
            "mem_total": num(p[7]), "clock": num(p[8]),
            "reasons": int(p[9], 16) if p[9].startswith("0x") else 0}


def _cpu_util():
    vals = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    idle, total = vals[3] + vals[4], sum(vals)
    prev, _prev["stat"] = _prev["stat"], (idle, total)
    if not prev or total == prev[1]:
        return 0.0
    return 100 * (1 - (idle - prev[0]) / (total - prev[1]))


def _cpu_power(util):
    """Real RAPL reading if readable, otherwise an estimate from utilization."""
    energy, now = _read("/sys/class/powercap/intel-rapl:0/energy_uj", int), time.time()
    if energy is not None:
        last, _prev["rapl"] = _prev["rapl"], (energy, now)
        if last and energy >= last[0] and now > last[1]:
            return (energy - last[0]) / 1e6 / (now - last[1]), True
    return CPU_IDLE_W + (CPU_MAX_W - CPU_IDLE_W) * (util / 100) ** 0.9, False


def _alerts():
    """Kernel/system alerts since boot and failed units, refreshed every 30 s."""
    if time.time() - _logs["t"] < 30:
        return _logs["events"], _logs["failed"]
    events = []
    try:
        out = subprocess.run(["journalctl", "-b", "-p", "0..4", "--no-pager", "-o", "short-iso", "-n", "2000"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    for line in out.splitlines():
        m = re.match(r"(\S+)\s+\S+\s+([^:]+):\s*(.*)", line)
        if not m or NOISE.search(line):
            continue
        ts, src, msg = m.groups()
        lvl = "bad" if CRIT_PATTERNS.search(msg) else "warn" if WARN_PATTERNS.search(msg) else None
        if lvl:
            events.append((ts[11:16], lvl, src.split("[")[0], msg))
    failed = []
    for cmd in (["systemctl"], ["systemctl", "--user"]):
        try:
            r = subprocess.run([*cmd, "--failed", "--no-legend", "--plain"], capture_output=True, text=True, timeout=3)
            failed += [l.split()[0] for l in r.stdout.splitlines() if l.strip()]
        except (OSError, subprocess.TimeoutExpired):
            pass
    _logs.update(t=time.time(), events=events[-8:], failed=failed)
    return _logs["events"], failed


def sample(cfg):
    util = _cpu_util()
    t, g = _temps(), _gpu()
    cpu_w, rapl = _cpu_power(util)
    gpu_w = g["power"] if g else 0
    dc = cpu_w + gpu_w + BASE_W
    mem = {k: int(v.split()[0]) * 1024 for k, v in (l.split(":", 1) for l in open("/proc/meminfo"))}
    disk = shutil.disk_usage("/")
    events, failed = _alerts()
    hist["cpu"].append(t.get("cpu"))
    hist["gpu"].append(g["temp"] if g else None)
    hist["watts"].append(dc / PSU_EFFICIENCY)
    STATE["machine"] = {
        "at": time.time(), "temps": t, "gpu": g, "cpu_util": util, "cpu_w": cpu_w, "rapl": rapl,
        "dc": dc, "wall": dc / PSU_EFFICIENCY, "psu_load": dc / cfg.get("psu_watts", PSU_WATTS),
        "mem_used": mem["MemTotal"] - mem["MemAvailable"], "mem_total": mem["MemTotal"],
        "disk_used": disk.used, "disk_total": disk.total, "load": os.getloadavg()[0],
        "uptime": float(open("/proc/uptime").read().split()[0]), "events": events, "failed": failed,
    }


def card(cfg):
    s = STATE.get("machine")
    if not s:
        sample(cfg)
        s = STATE["machine"]
    t, g = s["temps"], s["gpu"] or {}
    out = ["<h3>Temperatures</h3>"]
    for label, v, key, scale in (("CPU", t.get("cpu"), "cpu", 100), ("GPU", g.get("temp"), "gpu", 100),
                                 ("NVMe", t.get("nvme"), "nvme", 90), ("Board", t.get("board"), "board", 90)):
        lvl = level(v, *LIMITS[key])
        out.append(f'<div class="meter"><span>{label}</span><b class="{lvl}">'
                   f'{f"{v:.0f}°" if v is not None else "n/a"}</b>{bar((v or 0) / scale, lvl)}</div>')
    out.append(f'<div class="sparks"><span>CPU 3 min</span>{spark(hist["cpu"], 30, 95, "violet")}'
               f'<span>GPU 3 min</span>{spark(hist["gpu"], 30, 95, "green")}</div>')

    w, c = PSU_LOAD_LIMITS
    load_lvl = "bad" if s["psu_load"] >= c else "warn" if s["psu_load"] >= w else "ok"
    out.append("<h3>Power</h3>")
    out.append(f'<div class="meter"><span>Wall</span><b class="{load_lvl}">{s["wall"]:.0f} W</b>'
               f'{bar(s["psu_load"], load_lvl)}</div>')
    out.append(row("PSU load", e(f'{s["psu_load"] * 100:.0f}% of {cfg.get("psu_watts", PSU_WATTS)} W (DC {s["dc"]:.0f} W)')))
    out.append(row("CPU", e(f'{s["cpu_w"]:.0f} W {"(RAPL)" if s["rapl"] else "(est.)"}')))
    if g:
        out.append(row("GPU", e(f'{g["power"]:.0f} / {g["limit"]:.0f} W')))
    out.append(f'<div class="sparks one"><span>Wall 3 min</span>{spark(hist["watts"], 0, 400, "amber")}</div>')

    out.append("<h3>Usage</h3>")
    out.append(row("CPU", e(f'{s["cpu_util"]:.0f}% · load {s["load"]:.2f}')))
    out.append(row("Memory", e(f'{s["mem_used"] / 2**30:.1f} / {s["mem_total"] / 2**30:.0f} GB')))
    out.append(row("Disk /", e(f'{s["disk_used"] / 2**30:.0f} / {s["disk_total"] / 2**30:.0f} GB')))
    if g:
        out.append(row(g["name"], e(f'{g["util"]:.0f}% · {g["mem_used"] / 1024:.1f}/{g["mem_total"] / 1024:.0f} GB'
                                    f' · {g["clock"]:.0f} MHz · fan {g["fan"]:.0f}%')))

    alerts = [(lvl, txt) for bit, (txt, lvl) in THROTTLE_BITS.items() if g.get("reasons", 0) & bit]
    alerts += [("bad", f"Failed unit: {u}") for u in s["failed"]]
    alerts += [(lvl, f"{ts} {src}: {msg}") for ts, lvl, src, msg in s["events"]]
    out.append("<h3>Alerts</h3>")
    if alerts:
        out += [f'<div class="alert {lvl}">{e(txt[:200])}</div>' for lvl, txt in alerts]
    else:
        out.append(f'<div class="row"><span>Since boot</span>{pill(True, "all clear")}</div>')
    return "".join(out)
