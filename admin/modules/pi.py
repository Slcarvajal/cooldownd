"""Raspberry Pi health at a glance: temperature, throttling, load, memory, disk, uptime,
plus a 3-minute temperature graph, fan, swap, supply voltage and the NVMe drive once there is one."""
import glob
import os
import shutil
from collections import deque

from modules.common import ago, e, level, pill, row, run, spark

NAME, TITLE, TAB = "pi", "Raspberry Pi", "home"
SAMPLE_EVERY = 2
LIMITS = {"temp": (70, 80), "nvme": (65, 75), "mem": (0.80, 0.92), "disk": (0.80, 0.92)}  # (warn, crit)
# vcgencmd get_throttled bits that mean "happening now"; the same bits << 16 mean "since boot"
THROTTLED = {0x1: "under-voltage (power supply!)", 0x2: "CPU speed capped", 0x4: "throttled", 0x8: "temperature limit"}
VOLT_LOW = 4.85  # the Pi 5 flags under-voltage below ~4.63 V on its 5 V rail; warn before that
hist = deque(maxlen=90)  # 3 minutes at one sample every 2 s


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _hwmon(name, field):
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(f"{d}/name") == name:
            v = _read(f"{d}/{field}")
            return int(v) if v and v.lstrip("-").isdigit() else None
    return None


def _temp():
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    return int(t) / 1000 if t else None


def _volts():
    out = run("vcgencmd", "pmic_read_adc", "EXT5V_V")  # "EXT5V_V volt(24)=5.16436000V"
    try:
        return float(out.rsplit("=", 1)[1].rstrip("V"))
    except (IndexError, ValueError):
        return None


def sample(cfg):
    hist.append(_temp())


def _tile(label, value, lvl="", sub=""):
    """A stat tile: grey when calm, tinted when it's warn or bad."""
    tint = f" tint-{lvl}" if lvl in ("warn", "bad") else ""
    return (f'<div class="tile{tint}"><small>{e(label)}</small><b class="{lvl}">{e(value)}</b>'
            f'{f"<small>{e(sub)}</small>" if sub else ""}</div>')


def card(cfg):
    temp = _temp()
    load1 = os.getloadavg()[0]
    mem = dict((l.split(":")[0], int(l.split()[1]) * 1024) for l in open("/proc/meminfo") if l.split()[1:2])
    ram_used = mem["MemTotal"] - mem["MemAvailable"]
    mem_frac = ram_used / mem["MemTotal"]
    du = shutil.disk_usage("/")
    disk_frac = du.used / du.total
    up = float((_read("/proc/uptime") or "0").split()[0])
    gb = 2**30
    tiles = [
        _tile("Temperature", f"{temp:.0f}°" if temp is not None else "n/a", level(temp, *LIMITS["temp"]),
              f"up {ago(up)}"),
        _tile("CPU load", f"{load1:.2f}", "warn" if load1 >= os.cpu_count() else "ok", f"{os.cpu_count()} cores"),
        _tile("Memory", f"{mem_frac * 100:.0f}%", level(mem_frac, *LIMITS["mem"]),
              f"{ram_used / gb:.1f} / {mem['MemTotal'] / gb:.1f} GB"),
        _tile("Disk", f"{disk_frac * 100:.0f}%", level(disk_frac, *LIMITS["disk"]),
              f"{du.used / gb:.0f} / {du.total / gb:.0f} GB"),
    ]
    out = [f'<div class="tiles">{"".join(tiles)}</div>',
           f'<div class="sparks one"><span>Temp 3 min</span>{spark(hist, 30, 90, "violet")}</div>']

    nvme = _hwmon("nvme", "temp1_input")
    if nvme is not None:
        out.append(row("NVMe", f'<b class="{level(nvme / 1000, *LIMITS["nvme"])}">{nvme / 1000:.0f}°</b>'))
    fan = _hwmon("pwmfan", "fan1_input")
    if fan is not None:
        out.append(row("Fan", e(f"{fan} rpm" if fan else "off (cool enough)")))
    mhz = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if mhz:
        out.append(row("CPU clock", e(f"{int(mhz) / 1000:.0f} MHz")))
    if mem["SwapTotal"]:
        out.append(row("Swap", e(f"{(mem['SwapTotal'] - mem['SwapFree']) / gb:.1f} / {mem['SwapTotal'] / gb:.1f} GB")))
    volts = _volts()
    if volts is not None:
        out.append(row("5 V supply", f'<b class="{"warn" if volts < VOLT_LOW else "ok"}">{volts:.2f} V</b>'))

    raw = run("vcgencmd", "get_throttled").partition("=")[2]
    flags = int(raw, 16) if raw else 0
    now = [msg for bit, msg in THROTTLED.items() if flags & bit]
    before = [msg for bit, msg in THROTTLED.items() if flags & (bit << 16) and not flags & bit]
    out += [f'<div class="alert bad">{e(msg)}</div>' for msg in now]
    out += [f'<div class="alert warn">Since boot: {e(msg)}</div>' for msg in before]
    if flags & 0x10000 and not flags & 0x1:
        out.append('<div class="alert warn">Use the official 27 W supply.</div>')
    if not now and not before:
        out.append(f'<div class="row"><span>Power and throttling</span><span>{pill(True, "fine")}</span></div>')
    return "".join(out)
