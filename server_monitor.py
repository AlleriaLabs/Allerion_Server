#!/usr/bin/env python3
"""Home server monitor: runs on the server, serves a phone-friendly dashboard.

Setup:   pip install psutil
Run:     TOKEN=choose-a-secret python3 server_monitor.py
Phone:   open http://<server-ip>:8080 in Chrome, then menu > Add to Home screen
"""
import calendar, glob, json, os, socket, subprocess, threading, time, platform, urllib.request, ipaddress, http.client, re
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import psutil
from collections import deque
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

PORT = int(os.environ.get("PORT", 8080))
TOKEN = os.environ.get("TOKEN", "")
if not TOKEN and os.environ.get("ALLOW_INSECURE_LOCAL", "0") != "1":
    raise RuntimeError("TOKEN is required. Set ALLOW_INSECURE_LOCAL=1 only for isolated local testing.")
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
SPEED_EVERY = int(os.environ.get("SPEEDTEST_MINUTES", 30)) * 60
PING_TARGET = ("1.1.1.1", 443)

# --- power settings (all optional) ---
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
BASE_WATTS = float(os.environ.get("BASE_WATTS", 0))   # extra draw the CPU reading cannot see (board, drives, fans)
PRICE = float(os.environ.get("PRICE_PER_KWH", 0))
CURRENCY = os.environ.get("CURRENCY", "")
PLUG_URL = os.environ.get("PLUG_URL", "")             # smart plug status URL
PLUG_TYPE = os.environ.get("PLUG_TYPE", "shelly1")    # shelly1 | shelly2 | tasmota
try:
    TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
except Exception:
    TZ = timezone.utc

stats = {}
speed = {"down_mbps": None, "up_mbps": None, "ping_ms": None, "server": None, "ts": None, "running": False, "error": None,
         "phase": None, "progress": 0.0}
_speed_lock = threading.Lock()


def cpu_temp():
    """Return the hottest available host temperature in Celsius.

    Prefer psutil's hardware sensor support, then fall back to Linux sysfs.
    The Docker deployment bind-mounts the host root at /hostfs, so host
    thermal-zone and hwmon readings may be visible there even when the
    container's own /sys namespace has no sensors.
    """
    values = []
    try:
        sensors = psutil.sensors_temperatures()
        for group in sensors.values():
            for sensor in group:
                current = getattr(sensor, "current", None)
                if isinstance(current, (int, float)) and -50 <= current <= 200:
                    values.append(float(current))
    except (AttributeError, OSError, RuntimeError):
        pass

    # Linux thermal zones and hwmon inputs report millidegrees Celsius.
    # Check both container-visible and host-mounted sysfs locations.
    patterns = (
        "/sys/class/thermal/thermal_zone*/temp",
        "/sys/class/hwmon/hwmon*/temp*_input",
        "/hostfs/sys/class/thermal/thermal_zone*/temp",
        "/hostfs/sys/class/hwmon/hwmon*/temp*_input",
    )
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                with open(path, encoding="ascii") as handle:
                    raw = float(handle.read().strip())
                current = raw / 1000.0 if abs(raw) > 1000 else raw
                if -50 <= current <= 200:
                    values.append(current)
            except (OSError, ValueError):
                continue

    return round(max(values), 1) if values else None


def ping_ms():
    try:
        t = time.time()
        socket.create_connection(PING_TARGET, timeout=2).close()
        return round((time.time() - t) * 1000, 1)
    except OSError:
        return None


# Storage discovery reads the HOST mount table, not the container's overlay mounts.
# The Docker deployment bind-mounts / at /hostfs with rslave propagation.
_HOST_ROOT = "/hostfs"
_PSEUDO_FILESYSTEMS = {
    "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs",
    "debugfs", "devpts", "devtmpfs", "efivarfs", "fusectl", "hugetlbfs",
    "mqueue", "nsfs", "overlay", "proc", "pstore", "ramfs", "securityfs",
    "squashfs", "sysfs", "tmpfs", "tracefs",
}


def _unescape_mount_field(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _host_mountinfo():
    # PID 1 is the host init process because Compose uses pid: host.
    for mount_table in ("/hostfs/proc/1/mountinfo", "/proc/1/mountinfo", "/proc/self/mountinfo"):
        try:
            with open(mount_table, encoding="utf-8") as handle:
                return handle.readlines()
        except OSError:
            continue
    return []


def _host_disk_identity(major_minor, source):
    sys_link = os.path.join("/hostfs/sys/dev/block", major_minor)
    try:
        sys_path = os.path.realpath(sys_link)
        if not os.path.exists(sys_link):
            raise OSError("sysfs block device unavailable")
        name = os.path.basename(sys_path)
        if os.path.exists(os.path.join(sys_path, "partition")):
            name = os.path.basename(os.path.dirname(sys_path))
        removable = False
        removable_file = os.path.join("/hostfs/sys/class/block", os.path.basename(sys_path), "removable")
        try:
            with open(removable_file, encoding="ascii") as handle:
                removable = handle.read().strip() == "1"
        except OSError:
            pass
        usb = "/usb" in sys_path.lower()
        return name, sys_path, removable or usb, "USB" if usb else "Internal"
    except OSError:
        return source, "", False, "Unknown"


def disks():
    out = []
    seen = set()
    for line in _host_mountinfo():
        try:
            left, right = line.rstrip("\n").split(" - ", 1)
            fields = left.split()
            tail = right.split()
            if len(fields) < 5 or len(tail) < 2:
                continue
            major_minor = fields[2]
            mountpoint = _unescape_mount_field(fields[4])
            filesystem = tail[0]
            source = _unescape_mount_field(tail[1])
            # Only real block-backed filesystems belong in physical storage totals.
            if filesystem in _PSEUDO_FILESYSTEMS or not source.startswith("/dev/"):
                continue
            identity = (major_minor, _unescape_mount_field(fields[3]))
            if identity in seen:
                continue
            host_path = os.path.normpath(os.path.join(_HOST_ROOT, mountpoint.lstrip("/")))
            if not os.path.exists(host_path):
                continue
            # Ignore host mount-table entries whose submount was not propagated into
            # the container; otherwise disk_usage would report the parent filesystem.
            if mountpoint != "/" and not os.path.ismount(host_path):
                continue
            usage = psutil.disk_usage(host_path)
            disk_name, sys_path, removable, connection = _host_disk_identity(major_minor, source)
            seen.add(identity)
            out.append({
                "id": major_minor,
                "mount": mountpoint,
                "pct": usage.percent,
                "used_gb": round(usage.used / 1e9, 1),
                "total_gb": round(usage.total / 1e9, 1),
                "free_gb": round(usage.free / 1e9, 1),
                "device": source,
                "disk": disk_name,
                "filesystem": filesystem,
                "disk_id": disk_name,
                "removable": removable,
                "connection": connection,
            })
        except (OSError, ValueError, IndexError):
            continue

    # Compatibility fallback for non-Docker/manual deployments where host mountinfo
    # is unavailable. Never add these on top of discovered host mounts.
    if not out:
        for part in psutil.disk_partitions(all=False):
            if not part.device.startswith("/dev/") or part.fstype in _PSEUDO_FILESYSTEMS:
                continue
            path = part.mountpoint
            if not path.startswith(_HOST_ROOT):
                path = os.path.normpath(os.path.join(_HOST_ROOT, path.lstrip("/")))
            try:
                usage = psutil.disk_usage(path)
                disk_name, _, removable, connection = _host_disk_identity("0:0", part.device)
                out.append({
                    "id": part.device, "mount": part.mountpoint, "pct": usage.percent,
                    "used_gb": round(usage.used / 1e9, 1), "total_gb": round(usage.total / 1e9, 1),
                    "free_gb": round(usage.free / 1e9, 1), "device": part.device,
                    "disk": disk_name, "filesystem": part.fstype, "disk_id": disk_name,
                    "removable": removable, "connection": connection,
                })
            except OSError:
                continue
    return out


RAPL_DIRS = sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]"))


def rapl_uj():
    return sum(int(open(d + "/energy_uj").read()) for d in RAPL_DIRS)


def rapl_max():
    try:
        return sum(int(open(d + "/max_energy_range_uj").read()) for d in RAPL_DIRS)
    except (OSError, ValueError):
        return 2 ** 32


PLUG_LABELS = {"tuya": "Smart plug (Tuya)", "shelly1": "Smart plug (Shelly)",
               "shelly2": "Smart plug (Shelly)", "tasmota": "Smart plug (Tasmota)",
               "tapo": "Smart plug (Tapo P110M)"}
def write_private_json(path, value):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


CFG_FILE = os.path.join(DATA_DIR, "power_config.json")
CONFIG = {"source": PLUG_TYPE if PLUG_URL else "cpu", "host": PLUG_URL, "tuya_id": "", "tuya_key": "",
          "tuya_version": "3.3", "tuya_dp": "19", "tuya_voltage_dp": "20",
          "tapo_user": os.environ.get("TAPO_USER", ""), "tapo_pass": os.environ.get("TAPO_PASS", ""), "tuya_scale": 0.1,
          "base_watts": BASE_WATTS, "price": PRICE, "currency": CURRENCY}
try:
    CONFIG.update(json.load(open(CFG_FILE)))
except Exception:
    pass
_plug = {"t": 0.0, "w": None, "v": None, "err": None}


def clean_cfg(d):
    out = {}
    for k in ("source", "host", "tuya_id", "tuya_key", "tuya_version", "tuya_dp", "tuya_voltage_dp", "tapo_user", "tapo_pass", "currency"):
        if k in d:
            out[k] = str(d[k]).strip()
    for k in ("tuya_scale", "base_watts", "price"):
        if k in d:
            try:
                out[k] = float(d[k])
            except (TypeError, ValueError):
                pass
    if out.get("source") not in (None, "cpu", "tuya", "tapo", "shelly1", "shelly2", "tasmota"):
        out.pop("source")
    return out


def plug_url(src, host):
    if host.startswith("http"):
        return host
    return {"shelly1": f"http://{host}/status", "shelly2": f"http://{host}/rpc/Switch.GetStatus?id=0",
            "tasmota": f"http://{host}/cm?cmnd=Status%208"}[src]


def read_plug(c):
    """Returns (watts, volts_or_None, raw_data). Raises on any problem, with a readable message."""
    src, host = c["source"], c["host"]
    if not host:
        raise RuntimeError("Enter the plug IP address")
    if src == "tuya":
        import tinytuya
        if not (c["tuya_id"] and c["tuya_key"]):
            raise RuntimeError("Enter the Device ID and Local Key")
        d = tinytuya.OutletDevice(dev_id=c["tuya_id"], address=host, local_key=c["tuya_key"],
                                  version=float(c["tuya_version"]))
        try:
            d.set_socketTimeout(3)
            d.set_socketRetryLimit(1)
        except Exception:
            pass
        r = d.status()
        if "dps" not in r:
            raise RuntimeError(str(r.get("Error") or r)[:120])
        v = r["dps"].get(str(c["tuya_dp"]))
        if v is None:
            raise RuntimeError("Data point %s not found. Available: %s" % (c["tuya_dp"], json.dumps(r["dps"])))
        vv = r["dps"].get(str(c.get("tuya_voltage_dp") or "20"))
        return float(v) * float(c["tuya_scale"]), (None if vv is None else float(vv) / 10.0), r["dps"]
    if src == "tapo":
        if not (c.get("tapo_user") and c.get("tapo_pass")):
            raise RuntimeError("Enter your TP-Link account email and password")
        import asyncio
        from kasa import Discover, Credentials

        async def go():
            dev = await Discover.discover_single(host, credentials=Credentials(c["tapo_user"], c["tapo_pass"]))
            try:
                await dev.update()
                p = v = None
                e = dev.modules.get("Energy") if hasattr(dev, "modules") else None
                if e is not None:
                    p, v = getattr(e, "current_consumption", None), getattr(e, "voltage", None)
                if p is None:
                    st = dev.emeter_realtime
                    p, v = st.power, getattr(st, "voltage", v)
                return p, v, {"model": str(dev.model), "name": str(dev.alias), "power_w": p, "voltage_v": v}
            finally:
                await dev.disconnect()

        p, v, raw = asyncio.run(go())
        if p is None:
            raise RuntimeError("This plug did not report power")
        return float(p), (None if v is None else float(v)), raw
    with urllib.request.urlopen(plug_url(src, host), timeout=3) as r:
        d = json.load(r)
    if src == "tasmota":
        e = d["StatusSNS"]["ENERGY"]
        w, v = e["Power"], e.get("Voltage")
    elif src == "shelly2":
        w, v = d["apower"], d.get("voltage")
    else:
        w, v = d["meters"][0]["power"], d.get("voltage")
    return float(w), (None if v is None else float(v)), d


def plug_watts():
    c = CONFIG
    if c["source"] == "cpu" or not c["host"]:
        return None
    now = time.time()
    if now - _plug["t"] < (5 if c["source"] in ("tuya", "tapo") else 2):
        return _plug["w"]
    _plug["t"] = now
    try:
        _plug["w"], _plug["v"], _ = read_plug(c)
        _plug["err"] = None
    except Exception as e:
        _plug["w"], _plug["v"], _plug["err"] = None, None, (str(e) or e.__class__.__name__)[:140]
    return _plug["w"]


class Power:
    """Measures watts and adds them up into kWh per day (saved to energy.json)."""

    def __init__(self):
        self.file = os.path.join(DATA_DIR, "energy.json")
        try:
            self.hist = json.load(open(self.file))
        except Exception:
            self.hist = {}
        self.last_uj = self.last_t = self.prev_t = None
        self.prev_watts = None
        self.prev_source = None
        self.source_file = os.path.join(DATA_DIR, "energy_sources.json")
        try:
            self.source_hist = json.load(open(self.source_file))
        except Exception:
            self.source_hist = {}
        self.rmax = rapl_max() if RAPL_DIRS else 0
        self.saved = time.time()

    def sample(self):
        now = time.time()
        cpu_w = None
        if RAPL_DIRS:
            try:
                uj = rapl_uj()
                if self.last_uj is not None and now > self.last_t:
                    d = uj - self.last_uj
                    if d < 0:
                        d += self.rmax
                    cpu_w = d / 1e6 / (now - self.last_t)
                self.last_uj, self.last_t = uj, now
            except (OSError, ValueError):
                pass
        watts, source = plug_watts(), PLUG_LABELS.get(CONFIG["source"], "Smart plug")
        if watts is None and cpu_w is not None:
            watts = cpu_w + CONFIG["base_watts"]
            source = "CPU + base estimate" if CONFIG["base_watts"] else "CPU only (RAPL)"
        if watts is None:
            source = None
        local = datetime.now(TZ)
        today = local.strftime("%Y-%m-%d")
        if watts is not None and self.prev_t and self.prev_watts is not None and self.prev_source:
            elapsed = min(now - self.prev_t, 10)
            kwh = self.prev_watts * elapsed / 3.6e6
            self.hist[today] = self.hist.get(today, 0.0) + kwh
            day_sources = self.source_hist.setdefault(today, {})
            day_sources[self.prev_source] = day_sources.get(self.prev_source, 0.0) + kwh
        self.prev_t, self.prev_watts, self.prev_source = now, watts, source
        if now - self.saved > 60:
            self.saved = now
            for k in sorted(self.hist)[:-400]:
                del self.hist[k]
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                json.dump(self.hist, open(self.file, "w"))
                for k in sorted(self.source_hist)[:-400]:
                    del self.source_hist[k]
                json.dump(self.source_hist, open(self.source_file, "w"))
            except OSError:
                pass
        days = [(local - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        t = self.hist.get(today, 0.0)
        return {"watts": None if watts is None else round(watts, 1), "source": source,
                "today_kwh": round(t, 3), "price": CONFIG["price"], "currency": CONFIG["currency"],
                "plug_type": CONFIG["source"], "plug_error": _plug["err"],
                "voltage": _plug["v"] if (watts is not None and CONFIG["source"] != "cpu") else None,
                "year_kwh": round(sum(v for k, v in self.hist.items() if k[:4] == today[:4]), 2),
                "since": min(self.hist) if self.hist else today,
                "days_in_month": calendar.monthrange(local.year, local.month)[1],
                "month_history": [{"day": "%s%02d" % (today[:8], d), "kwh": round(self.hist.get("%s%02d" % (today[:8], d), 0.0), 3)}
                                  for d in range(1, local.day + 1)],
                "month_kwh": round(sum(v for k, v in self.hist.items() if k[:7] == today[:7]), 2),
                "history": [{"day": d, "kwh": round(self.hist.get(d, 0.0), 3)} for d in days]}


def gpu_usage():
    for f in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        try:
            return float(open(f).read().strip())
        except (OSError, ValueError):
            pass
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            return float(r.stdout.split()[0])
    except Exception:
        pass
    return None


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


_pcache = {}


def top_procs(n=8):
    agg, seen = {}, set()
    cores = psutil.cpu_count() or 1
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            seen.add(p.pid)
            pr = _pcache.setdefault(p.pid, p)
            cpu = pr.cpu_percent(None) / cores
            mi = p.info["memory_info"]
            a = agg.setdefault(p.info["name"] or "?", [0.0, 0])
            a[0] += cpu
            a[1] += mi.rss if mi else 0
        except psutil.Error:
            pass
    for pid in list(_pcache):
        if pid not in seen:
            del _pcache[pid]
    rows = [{"name": k, "cpu": round(v[0], 1), "mem_mb": round(v[1] / 1e6)} for k, v in agg.items()]
    rows.sort(key=lambda r: r["mem_mb"], reverse=True)
    return rows[:n]


HIST_FILE = os.path.join(DATA_DIR, "history.json")
METER = None


class History:
    """Rolling real readings: 10 s points for 1 h, 1 min for 24 h, 10 min for 7 days. Missing readings stay None."""
    KEYS = ("cpu", "ram", "temp", "down", "up", "power", "gpu", "disk")
    SPEC = (("fine", 10, 360), ("min", 60, 1440), ("ten", 600, 1008))
    WINDOW = {"1h": ("fine", 10, 3600), "24h": ("min", 60, 86400), "7d": ("ten", 600, 604800)}

    def __init__(self):
        self.lock = threading.Lock()
        self.buf = {n: deque(maxlen=m) for n, _, m in self.SPEC}
        self.acc = {n: [] for n, _, _ in self.SPEC}
        self.last = {n: time.time() for n, _, _ in self.SPEC}
        self.saved = time.time()
        try:
            d = json.load(open(HIST_FILE))
            for n, _, m in self.SPEC:
                self.buf[n].extend(d.get(n, [])[-m:])
        except Exception:
            pass

    def add(self, row):
        now = row["t"]
        with self.lock:
            for n, sec, _ in self.SPEC:
                self.acc[n].append(row)
                if now - self.last[n] >= sec:
                    rows, self.acc[n], self.last[n] = self.acc[n], [], now
                    out = {"t": int(now)}
                    for k in self.KEYS:
                        v = [r[k] for r in rows if r.get(k) is not None]
                        out[k] = round(sum(v) / len(v), 2) if v else None
                    self.buf[n].append(out)
            if now - self.saved > 300:
                self.saved = now
                try:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    json.dump({n: list(b) for n, b in self.buf.items()}, open(HIST_FILE, "w"))
                except OSError:
                    pass

    def get(self, rng):
        name, sec, window = self.WINDOW.get(rng, self.WINDOW["1h"])
        with self.lock:
            rows = [r for r in self.buf[name] if r["t"] >= time.time() - window]
        out, prev = [], None
        for r in rows:
            if prev is not None and r["t"] - prev > sec * 2.5:      # server was off: leave a gap, never a zero
                out.append({"t": prev + sec, **{k: None for k in self.KEYS}})
            out.append(r)
            prev = r["t"]
        res = {"range": rng, "interval": sec, "t": [r["t"] for r in out]}
        for k in self.KEYS:
            res[k] = [r.get(k) for r in out]
        return res


HIST = History()


def collector():
    last = psutil.net_io_counters()
    last_t = time.time()
    psutil.cpu_percent(None)
    n = 0
    ping = None
    global METER
    procs = []
    meter = METER = Power()
    while True:
        time.sleep(2)
        now = time.time()
        io = psutil.net_io_counters()
        dt = now - last_t
        down = (io.bytes_recv - last.bytes_recv) / dt
        up = (io.bytes_sent - last.bytes_sent) / dt
        last, last_t = io, now
        if n % 5 == 0:
            ping = ping_ms()
        if n % 3 == 0:
            procs = top_procs()
        n += 1
        vm = psutil.virtual_memory()
        power = meter.sample()
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except (AttributeError, OSError):
            load = None
        stats.update({
            "ts": int(now),
            "host": socket.gethostname(),
            "os": platform.platform(),
            "uptime_s": int(now - psutil.boot_time()),
            "cpu": psutil.cpu_percent(None),
            "cores": psutil.cpu_count(),
            "load": load,
            "ram": vm.percent,
            "ram_used_gb": round(vm.used / 1e9, 1),
            "ram_total_gb": round(vm.total / 1e9, 1),
            "temp": cpu_temp(),
            "net_down_bps": down * 8,
            "net_up_bps": up * 8,
            "ping_ms": ping,
            "speed": dict(speed),
            "disks": disks(),
            "gpu": gpu_usage(),
            "ip": local_ip(),
            "procs": procs,
            "power": power,
        })
        dk = stats["disks"]
        dtot = sum(x["total_gb"] for x in dk)
        HIST.add({"t": now, "cpu": stats["cpu"], "ram": vm.percent, "temp": stats["temp"], "down": down * 8, "up": up * 8,
                  "power": power["watts"], "gpu": stats["gpu"],
                  "disk": (sum(x["used_gb"] for x in dk) / dtot * 100) if dtot else None})
        evaluate_server_alerts(dict(stats))


DEFAULT_OOKLA_SERVER_ID = "14762"
selected_speed_server_id = DEFAULT_OOKLA_SERVER_ID


OOKLA_SERVERS_FILE = os.path.join(DATA_DIR, "ookla_servers.json")
_OOKLA_SERVERS_LOCK = threading.Lock()
_OOKLA_SERVERS_CACHE = {"servers": [], "fetched_at": 0, "last_attempt_at": 0}
OOKLA_SERVERS_TTL = 6 * 60 * 60
OOKLA_SERVERS_RETRY = 60 * 60


def list_ookla_servers():
    """Return the fixed Smart Baguio server; never call Ookla discovery."""
    return {
        "servers": [{
            "id": DEFAULT_OOKLA_SERVER_ID,
            "name": "Smart Baguio",
            "location": "Baguio City",
            "distance_km": None,
        }],
        "selected_server_id": DEFAULT_OOKLA_SERVER_ID,
        "cached": True,
        "stale": False,
    }

def _ookla_cli(server_id=None):
    """Official Ookla Speedtest CLI. Streams its JSON lines so the app can show real progress."""
    args = ["speedtest", "--accept-license", "--accept-gdpr", "-f", "jsonl"]
    if server_id is not None:
        args.extend(["--server-id", str(int(server_id))])
    p = subprocess.Popen(args,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timer = threading.Timer(180, p.kill)
    timer.start()
    result = None
    try:
        for line in p.stdout:
            try:
                j = json.loads(line)
            except ValueError:
                continue
            t = j.get("type")
            if t == "result":
                result = j
            elif t in ("ping", "download", "upload"):
                d = j.get(t, {})
                speed.update(phase=t, progress=round(float(d.get("progress", 0)), 2))
        p.wait(timeout=10)
    finally:
        timer.cancel()
        if p.poll() is None:
            p.kill()
    if result is None:
        raise RuntimeError((p.stderr.read() or "speedtest CLI failed").strip()[:120])
    srv = result.get("server", {})
    return (result["download"]["bandwidth"] * 8 / 1e6, result["upload"]["bandwidth"] * 8 / 1e6, result["ping"]["latency"],
            ("%s, %s" % (srv.get("name", ""), srv.get("location", ""))).strip(", "))


def _ookla_py():
    """Speedtest.net servers through the speedtest-cli python package."""
    import speedtest
    st = speedtest.Speedtest(secure=True)
    st.get_best_server()
    st.download()
    st.upload()
    r = st.results.dict()
    return r["download"] / 1e6, r["upload"] / 1e6, r["ping"], "%s, %s" % (r["server"]["sponsor"], r["server"]["name"])


def _cf_down():
    t, got = time.time(), 0
    with urllib.request.urlopen(SPEED_URL, timeout=30) as r:
        while True:
            b = r.read(65536)
            if not b:
                break
            got += len(b)
    return got * 8 / (time.time() - t) / 1e6, None, None, "Cloudflare (download only)"


def run_speedtest(server_id=None):
    global selected_speed_server_id
    # Always pin tests to Smart Baguio, ignoring any client-supplied server ID.
    selected_speed_server_id = DEFAULT_OOKLA_SERVER_ID
    chosen_server = DEFAULT_OOKLA_SERVER_ID
    if not _speed_lock.acquire(blocking=False):
        return
    speed.update(running=True, error=None, phase="ping", progress=0.0)
    try:
        errs = []
        runners = [lambda: _ookla_cli(chosen_server)] if chosen_server else [_ookla_cli, _ookla_py, _cf_down]
        for runner in runners:
            try:
                d, u, p, srv = runner()
                speed.update(down_mbps=round(d, 1), up_mbps=None if u is None else round(u, 1),
                             ping_ms=None if p is None else round(p, 1), server=srv, error=None)
                break
            except Exception as e:
                errs.append("Ookla: %s" % (str(e) or e.__class__.__name__))
        else:
            speed.update(down_mbps=None, up_mbps=None, ping_ms=None, error=" | ".join(errs)[:200])
    finally:
        speed.update(running=False, phase=None, progress=0.0, ts=int(time.time()))
        _speed_lock.release()


def speed_loop():
    while True:
        run_speedtest()
        time.sleep(SPEED_EVERY)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALLERION</title><link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#101820">
<style>
:root{--bg:#eef2f1;--fg:#15222b;--mut:#5d717c;--line:#cfdad8;--ok:#0f8f84;--warn:#b7791f;--bad:#c4402f}
@media(prefers-color-scheme:dark){:root{--bg:#101820;--fg:#e6edf0;--mut:#8aa0ab;--line:#24343f;--ok:#5fd0c5;--warn:#e8b04a;--bad:#ef7a6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
main{max-width:640px;margin:0 auto;padding:18px 18px 40px}
header{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
h1{font-size:1.15rem;margin:0;font-weight:650}.sub{color:var(--mut);font-size:.82rem}
section{border-top:1px solid var(--line);padding:14px 0 12px}
.row{display:flex;justify-content:space-between;align-items:baseline}
.lab{color:var(--mut);font-size:.9rem}
.big{font-size:2.5rem;font-weight:300;font-variant-numeric:tabular-nums;letter-spacing:-.02em;transition:color .3s}
.big small{font-size:1rem;color:var(--mut);margin-left:3px;letter-spacing:0}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
svg{display:block;width:100%;height:38px;margin-top:4px}svg polyline{fill:none;stroke:currentColor;stroke-width:1.5;vector-effect:non-scaling-stroke;opacity:.85}
.bar{height:6px;background:var(--line);border-radius:3px;margin:6px 0 10px;overflow:hidden}.bar i{display:block;height:100%;background:currentColor;transition:width .4s}
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}.mid{font-size:1.5rem;font-variant-numeric:tabular-nums}
#msg{color:var(--bad);padding:30px 0;text-align:center}
</style></head><body><main>
<header><h1 id="host">Server</h1><span class="sub" id="up"></span></header>
<div id="msg" hidden></div><div id="app"></div></main>
<script>
const q=new URLSearchParams(location.search).get('token');if(q)localStorage.setItem('tok',q);
let tok=localStorage.getItem('tok')||'';
const H={cpu:[],ram:[],tmp:[],dn:[],up:[]},N=60;
const lvl=(v,w,b)=>v>=b?'bad':v>=w?'warn':'ok';
const push=(k,v)=>{H[k].push(v||0);if(H[k].length>N)H[k].shift()};
function spark(a,max){const m=max||Math.max(1,...a);
return '<svg viewBox="0 0 100 30" preserveAspectRatio="none"><polyline points="'+a.map((v,i)=>((N-a.length+i)/(N-1)*100).toFixed(1)+','+(29-Math.min(v/m,1)*27).toFixed(1)).join(' ')+'"/></svg>'}
const mbps=b=>(b/1e6).toFixed(b>=1e8?0:1);
function dur(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return(d?d+'d ':'')+h+'h '+m+'m'}
function block(label,val,unit,cls,hist,max,extra){return '<section><div class="row"><span class="lab">'+label+'</span><span class="lab">'+(extra||'')+'</span></div><div class="big '+cls+'" style="color:var(--'+cls+')">'+val+'<small>'+unit+'</small></div><div style="color:var(--'+cls+')">'+spark(hist,max)+'</div></section>'}
function draw(s){
document.getElementById('host').textContent=s.host;document.getElementById('up').textContent='up '+dur(s.uptime_s);
push('cpu',s.cpu);push('ram',s.ram);push('tmp',s.temp);push('dn',s.net_down_bps);push('up',s.net_up_bps);
let h=block('Processor',s.cpu.toFixed(0),'%',lvl(s.cpu,70,90),H.cpu,100,s.cores+' cores'+(s.load?' · load '+s.load[0]:''));
h+=block('Memory',s.ram.toFixed(0),'%',lvl(s.ram,75,90),H.ram,100,s.ram_used_gb+' of '+s.ram_total_gb+' GB');
h+=s.temp==null?'<section><div class="lab">Temperature</div><div class="lab">No sensor found on this machine</div></section>':block('Temperature',s.temp.toFixed(0),'°C',lvl(s.temp,70,85),H.tmp,100);
const sp=s.speed,pg=s.ping_ms;
h+='<section><div class="lab">Network now</div><div class="two"><div><div class="mid">↓ '+mbps(s.net_down_bps)+'<small class="lab"> Mbps</small></div>'+spark(H.dn)+'</div><div><div class="mid">↑ '+mbps(s.net_up_bps)+'<small class="lab"> Mbps</small></div>'+spark(H.up)+'</div></div></section>';
h+='<section><div class="lab">Internet</div><div class="two"><div><div class="mid">'+(sp.down_mbps==null?'testing…':sp.down_mbps+' <small class="lab">Mbps</small>')+'</div><div class="sub lab">download'+(sp.ts?' · '+new Date(sp.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'')+'</div></div><div><div class="mid '+(pg==null?'bad':lvl(pg,80,200))+'" style="color:var(--'+(pg==null?'bad':lvl(pg,80,200))+')">'+(pg==null?'offline':pg+' <small class="lab">ms</small>')+'</div><div class="sub lab">latency</div></div></div></section>';
h+='<section><div class="lab">Storage</div>'+s.disks.map(d=>'<div class="row" style="margin-top:8px"><span>'+d.mount+'</span><span class="lab">'+d.used_gb+' / '+d.total_gb+' GB</span></div><div class="bar" style="color:var(--'+lvl(d.pct,80,92)+')"><i style="width:'+d.pct+'%"></i></div>').join('')+'</section>';
document.getElementById('app').innerHTML=h}
async function tick(){
try{const r=await fetch('/api/stats',{headers:{'X-Token':tok}});
if(r.status==401){tok=prompt('Enter the access token')||'';localStorage.setItem('tok',tok);return}
const s=await r.json();if(!s.ts){return}
document.getElementById('msg').hidden=true;draw(s)}
catch(e){const m=document.getElementById('msg');m.hidden=false;m.textContent='Cannot reach the server. Check that it is on and on the same network.'}}
tick();setInterval(tick,2000);
</script></body></html>"""

MANIFEST = {"name": "ALLERION", "short_name": "ALLERION", "start_url": "/",
            "display": "standalone", "background_color": "#101820", "theme_color": "#101820",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]}
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="#101820"/>'
        '<polyline points="12,62 30,62 38,30 52,78 62,48 70,56 88,56" fill="none" stroke="#5fd0c5" stroke-width="7" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>')



# --- Optional infrastructure collectors / additive API v2 ---
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DOCKER_API_URL = os.environ.get("DOCKER_API_URL", "").rstrip("/")
SERVICE_CHECKS = os.environ.get("SERVICE_CHECKS", "")
ENABLE_LAN_DISCOVERY = os.environ.get("ENABLE_LAN_DISCOVERY", "0") == "1"
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
ALERT_WEBHOOK_SECRET = os.environ.get("ALERT_WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ALERT_THRESHOLDS = {
    "cpu": float(os.environ.get("ALERT_CPU_THRESHOLD", 90)),
    "ram": float(os.environ.get("ALERT_RAM_THRESHOLD", 90)),
    "temp": float(os.environ.get("ALERT_TEMP_THRESHOLD", 85)),
    "disk": float(os.environ.get("ALERT_DISK_THRESHOLD", 92)),
}
_alert_states = {}
_service_cache = {"at": 0.0, "items": []}
_service_lock = threading.Lock()


def api_capabilities():
    docker = bool(DOCKER_API_URL) or os.path.exists(DOCKER_SOCKET)
    return {
        "api_version": 2,
        "features": {
            "containers": docker,
            "processes": True,
            "services": bool(SERVICE_CHECKS.strip()),
            "network_inventory": ENABLE_LAN_DISCOVERY,
            "network_diagnostics": True,
            "network_topology": ENABLE_LAN_DISCOVERY,
            "network_traffic": ENABLE_LAN_DISCOVERY and platform.system() == "Linux",
            "energy_breakdown": True,
            "energy_forecast": bool(METER and len(METER.hist) >= 3),
            "webhooks": bool(ALERT_WEBHOOK_URL),
            "telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        },
    }


def process_inventory(limit=30):
    limit = max(1, min(int(limit), 50))
    rows = []
    for proc in psutil.process_iter(["pid", "name", "username", "memory_info"]):
        try:
            info = proc.info
            rows.append({
                "pid": info["pid"],
                "name": (info.get("name") or "unknown")[:100],
                "user": (info.get("username") or "")[:64],
                "cpu_pct": round(proc.cpu_percent(None) / (psutil.cpu_count() or 1), 1),
                "memory_bytes": int(info["memory_info"].rss) if info.get("memory_info") else 0,
            })
        except (psutil.Error, OSError):
            continue
    rows.sort(key=lambda item: (item["cpu_pct"], item["memory_bytes"]), reverse=True)
    return {"items": rows[:limit], "total_seen": len(rows), "limit": limit}


def docker_request(path):
    if DOCKER_API_URL:
        request = urllib.request.Request(DOCKER_API_URL + path, headers={"User-Agent": "ALLERION/2"})
        with urllib.request.urlopen(request, timeout=4) as response:
            if response.status != 200:
                raise RuntimeError("Docker API returned HTTP %s" % response.status)
            return json.loads(response.read(2_000_000).decode("utf-8"))
    if not os.path.exists(DOCKER_SOCKET):
        raise FileNotFoundError("Docker API proxy/socket is not configured")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(3)
        sock.connect(DOCKER_SOCKET)
        request = "GET %s HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n" % path
        sock.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(sock)
        response.begin()
        raw = response.read(2_000_000)
        if response.status != 200:
            raise RuntimeError("Docker API returned HTTP %s" % response.status)
        return json.loads(raw.decode("utf-8"))
    finally:
        sock.close()


def container_inventory():
    rows = docker_request("/containers/json?all=1")
    items = []
    for row in rows[:100]:
        names = row.get("Names") or []
        items.append({
            "id": row.get("Id", "")[:12],
            "name": (names[0].lstrip("/") if names else row.get("Id", "")[:12])[:120],
            "image": (row.get("Image") or "")[:160],
            "state": row.get("State", "unknown"),
            "status": (row.get("Status") or "")[:120],
            "created": row.get("Created"),
            "labels": {k: v for k, v in (row.get("Labels") or {}).items()
                       if k.startswith("com.docker.compose.") and len(k) < 100},
        })
    return {"items": items, "count": len(items), "source": "Docker Engine API"}



def container_stats(container_id):
    if not re.fullmatch(r"[A-Fa-f0-9]{12,64}", container_id):
        raise ValueError("Invalid container ID")
    data = docker_request("/containers/%s/stats?stream=false" % container_id)
    cpu = data.get("cpu_stats", {})
    precpu = data.get("precpu_stats", {})
    cpu_delta = (cpu.get("cpu_usage", {}).get("total_usage", 0) -
                 precpu.get("cpu_usage", {}).get("total_usage", 0))
    sys_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    cpu_pct = (cpu_delta / sys_delta * online * 100) if sys_delta > 0 and cpu_delta >= 0 else None
    memory = data.get("memory_stats", {})
    usage = memory.get("usage")
    limit = memory.get("limit")
    return {"id": container_id[:12], "cpu_pct": round(cpu_pct, 2) if cpu_pct is not None else None,
            "memory_usage_bytes": usage, "memory_limit_bytes": limit,
            "memory_pct": round(usage / limit * 100, 2) if usage is not None and limit else None,
            "read": data.get("read"), "source": "Docker Engine stats"}

def configured_services():
    items = []
    for entry in SERVICE_CHECKS.split(";"):
        name, sep, url = entry.partition("=")
        if sep and name.strip() and url.strip():
            parsed = urlparse(url.strip())
            if parsed.scheme in ("http", "https") and parsed.hostname:
                items.append((name.strip()[:80], url.strip()))
    return items[:20]


def service_inventory():
    now = time.time()
    with _service_lock:
        if now - _service_cache["at"] < 30:
            return {"items": list(_service_cache["items"]), "cached": True}
    result = []
    for name, url in configured_services():
        started = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ALLERION-Service-Monitor/2"})
            with urllib.request.urlopen(req, timeout=3) as response:
                status = response.status
            result.append({"name": name, "status": "up" if 200 <= status < 400 else "degraded",
                           "http_status": status, "latency_ms": round((time.monotonic()-started)*1000, 1)})
        except Exception as exc:
            result.append({"name": name, "status": "down", "http_status": None,
                           "latency_ms": round((time.monotonic()-started)*1000, 1),
                           "error": exc.__class__.__name__})
    with _service_lock:
        _service_cache.update(at=now, items=result)
    return {"items": result, "cached": False}


def lan_inventory():
    if not ENABLE_LAN_DISCOVERY:
        raise PermissionError("LAN discovery is disabled; set ENABLE_LAN_DISCOVERY=1 to enable passive neighbor discovery")
    found = {}
    # Passive neighbor/ARP table only: no unsolicited LAN sweep.
    try:
        with open("/proc/net/arp", encoding="utf-8") as handle:
            next(handle, None)
            for line in handle:
                cols = line.split()
                if len(cols) >= 6 and cols[3] != "00:00:00:00:00:00":
                    ip, mac, iface = cols[0], cols[3].lower(), cols[5]
                    found[ip] = {"ip": ip, "mac": mac, "interface": iface, "source": "ARP cache"}
    except OSError:
        pass
    try:
        proc = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=2)
        for line in proc.stdout.splitlines():
            cols = line.split()
            if len(cols) < 2 or not re.fullmatch(r"\\d{1,3}(?:\\.\\d{1,3}){3}", cols[0]):
                continue
            ip = cols[0]
            mac = cols[cols.index("lladdr")+1].lower() if "lladdr" in cols and cols.index("lladdr")+1 < len(cols) else ""
            iface = cols[cols.index("dev")+1] if "dev" in cols and cols.index("dev")+1 < len(cols) else ""
            state = cols[-1]
            previous = found.get(ip, {})
            found[ip] = {
                "ip": ip,
                "mac": mac or previous.get("mac", ""),
                "interface": iface or previous.get("interface", ""),
                "state": state,
                "source": "neighbor cache",
            }
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Bridge forwarding database is passive L2 evidence. It may expose MACs
    # without an IP; keep those as diagnostics rather than inventing devices.
    bridge_macs = {}
    try:
        proc = subprocess.run(["bridge", "fdb", "show"], capture_output=True, text=True, timeout=2)
        for line in proc.stdout.splitlines():
            cols = line.split()
            if not cols or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", cols[0]):
                continue
            mac = cols[0].lower()
            if mac.startswith(("01:", "33:")) or mac == "00:00:00:00:00:00":
                continue
            iface = cols[cols.index("dev")+1] if "dev" in cols and cols.index("dev")+1 < len(cols) else ""
            entry = bridge_macs.setdefault(mac, {"mac": mac, "interfaces": []})
            if iface and iface not in entry["interfaces"]:
                entry["interfaces"].append(iface)
    except (OSError, subprocess.TimeoutExpired):
        pass

    for item in found.values():
        observations = bridge_macs.get(item.get("mac", ""))
        if observations:
            item["bridge_seen"] = True
            item["bridge_interfaces"] = observations["interfaces"]

    known_ips = {item.get("ip") for item in found.values()}
    known_macs = {item.get("mac") for item in found.values() if item.get("mac")}
    unmapped_macs = [entry for mac, entry in bridge_macs.items() if mac not in known_macs]
    return {
        "items": sorted(found.values(), key=lambda x: tuple(int(p) for p in x["ip"].split("."))),
        "count": len(found),
        "mode": "passive-neighbor-cache",
        "bridge_mac_count": len(bridge_macs),
        "unmapped_macs": unmapped_macs[:100],
        "note": "Bridge MAC observations have no IP mapping in the current neighbor cache; they are not counted as IP devices.",
    }

def network_topology(inventory):
    """Infer same-subnet gateway associations from the active IPv4 route.

    These are logical LAN associations, not verified switch-port, cable,
    or device-to-device links. Subnet membership is authoritative; interface
    labels are diagnostic because ARP and ip-neigh can report different
    interface names in host/container network namespaces.
    """
    gateway = None
    interface = None
    source_ip = None
    try:
        route = subprocess.run(["ip", "-4", "route", "show", "default"],
                               capture_output=True, text=True, timeout=2)
        for line in route.stdout.splitlines():
            cols = line.split()
            if not cols or cols[0] != "default":
                continue
            if "via" in cols and cols.index("via") + 1 < len(cols):
                gateway = cols[cols.index("via") + 1]
            if "dev" in cols and cols.index("dev") + 1 < len(cols):
                interface = cols[cols.index("dev") + 1]
            if "src" in cols and cols.index("src") + 1 < len(cols):
                source_ip = cols[cols.index("src") + 1]
            if gateway:
                break
    except (OSError, subprocess.TimeoutExpired):
        pass

    if not gateway or not interface:
        try:
            with open("/proc/net/route", encoding="ascii") as handle:
                next(handle, None)
                for line in handle:
                    cols = line.split()
                    if len(cols) >= 8 and cols[1] == "00000000" and (int(cols[3], 16) & 1):
                        gateway = gateway or socket.inet_ntoa(bytes.fromhex(cols[2])[::-1])
                        interface = interface or cols[0]
                        break
        except (OSError, ValueError):
            pass

    network = None
    if interface:
        try:
            addr_result = subprocess.run(
                ["ip", "-o", "-4", "addr", "show", "dev", interface],
                capture_output=True, text=True, timeout=2
            )
            for match in re.finditer(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3}/\d+)", addr_result.stdout):
                candidate = ipaddress.ip_interface(match.group(1)).network
                if gateway and ipaddress.ip_address(gateway) in candidate:
                    network = candidate
                    source_ip = source_ip or match.group(1).split("/")[0]
                    break
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

        if network is None:
            try:
                for addr in psutil.net_if_addrs().get(interface, []):
                    if addr.family == socket.AF_INET and addr.address and addr.netmask:
                        candidate = ipaddress.ip_interface("%s/%s" % (addr.address, addr.netmask)).network
                        if gateway and ipaddress.ip_address(gateway) in candidate:
                            network = candidate
                            source_ip = source_ip or addr.address
                            break
            except (ValueError, OSError):
                pass

    nodes = [dict(item) for item in inventory.get("items", [])]
    links = []
    subnet = str(network) if network else None
    note = "No usable IPv4 default gateway and interface were detected; only passive neighbor inventory is available."

    if gateway:
        gateway_node = next((item for item in nodes if item.get("ip") == gateway), None)
        if gateway_node is None:
            gateway_node = {
                "ip": gateway, "mac": "", "interface": interface or "",
                "state": "gateway", "source": "default route",
                "role": "gateway", "inferred": True,
            }
            nodes.append(gateway_node)
        else:
            gateway_node["role"] = "gateway"

        if network:
            for item in nodes:
                ip = item.get("ip", "")
                if ip == gateway or not item.get("mac"):
                    continue
                try:
                    if ipaddress.ip_address(ip) not in network:
                        continue
                except ValueError:
                    continue
                item["topology_status"] = "linked"
                item["topology_reason"] = "MAC address present in the passive neighbor cache and IP is inside the active subnet."
                links.append({
                    "source": gateway,
                    "target": ip,
                    "type": "logical_gateway_path",
                    "label": "Same LAN / default gateway",
                    "confidence": "medium",
                    "verified": False,
                    "subnet": subnet,
                })
            note = (
                "Logical gateway associations inferred from same-subnet neighbor entries. "
                "Interface labels are not used as a hard filter. These are not verified "
                "physical cables, switch ports, or device-to-device traffic paths."
            )
        else:
            note = (
                "Default gateway %s detected on %s, but its directly connected IPv4 subnet "
                "could not be verified; no device links were inferred."
            ) % (gateway, interface or "unknown interface")

    linked_ips = {link["target"] for link in links}
    for item in nodes:
        if item.get("ip") == gateway:
            item["topology_status"] = "gateway"
            item["topology_reason"] = "Active IPv4 default route."
        elif item.get("ip") not in linked_ips:
            if not item.get("mac"):
                item["topology_status"] = "unmapped"
                item["topology_reason"] = "No MAC address in the passive ARP / neighbor cache."
            elif not network:
                item["topology_status"] = "unmapped"
                item["topology_reason"] = "Active IPv4 subnet could not be determined."
            else:
                item["topology_status"] = "unmapped"
                item["topology_reason"] = "Device IP is outside the active interface subnet."
    bridge_data = inventory.get("unmapped_macs", [])
    return {
        "unmapped_macs": bridge_data,
        "bridge_mac_count": inventory.get("bridge_mac_count", 0),
        "unmapped_node_count": sum(1 for item in nodes if item.get("topology_status") == "unmapped"),
        "nodes": sorted(nodes, key=lambda x: tuple(int(p) for p in x.get("ip", "").split("."))
                        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", x.get("ip", "")) else ()),
        "links": links,
        "gateway": gateway,
        "interface": interface,
        "source_ip": source_ip,
        "subnet": subnet,
        "mode": "inferred-gateway-paths",
        "link_count": len(links),
        "note": note,
    }


# --- Live packet metadata collector (Linux AF_PACKET; payloads are never read) ---
_traffic_lock = threading.Lock()
_traffic_events = deque(maxlen=4096)
_traffic_state = {
    "status": "stopped", "interface": None, "source_ip": None,
    "error": None, "packet_count": 0, "last_packet_at": None,
    "last_packet_peer": None, "last_src": None, "last_dst": None,
    "last_direction": None, "thread": None, "retry_at": 0.0,
}


def _traffic_capture_worker(interface, source_ip):
    try:
        capture = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        capture.bind((interface, 0))
        capture.settimeout(1.0)
        with _traffic_lock:
            _traffic_state.update(status="active", interface=interface,
                                  source_ip=source_ip, error=None)
        while True:
            try:
                frame, address = capture.recvfrom(65535)
            except socket.timeout:
                continue
            if len(frame) < 14:
                continue
            ethertype = int.from_bytes(frame[12:14], "big")
            offset = 14
            while ethertype in (0x8100, 0x88A8, 0x9100):
                if len(frame) < offset + 4:
                    break
                ethertype = int.from_bytes(frame[offset + 2:offset + 4], "big")
                offset += 4
            else:
                src = dst = None
                if ethertype == 0x0800 and len(frame) >= offset + 20:
                    ihl = (frame[offset] & 0x0F) * 4
                    if ihl < 20 or len(frame) < offset + ihl:
                        continue
                    src = str(ipaddress.ip_address(frame[offset + 12:offset + 16]))
                    dst = str(ipaddress.ip_address(frame[offset + 16:offset + 20]))
                elif ethertype == 0x86DD and len(frame) >= offset + 40:
                    src = str(ipaddress.ip_address(frame[offset + 8:offset + 24]))
                    dst = str(ipaddress.ip_address(frame[offset + 24:offset + 40]))
                else:
                    continue

                # PACKET_OUTGOING is 4 on Linux. Other frames are received.
                packet_type = address[2] if isinstance(address, tuple) and len(address) > 2 else 0
                direction = "tx" if packet_type == 4 else "rx"
                peer = dst if direction == "tx" else src
                if not peer or peer == source_ip:
                    continue
                now = time.time()
                with _traffic_lock:
                    _traffic_events.append((now, peer, direction, len(frame), src, dst))
                    _traffic_state.update(
                        packet_count=_traffic_state["packet_count"] + 1,
                        last_packet_at=now, last_packet_peer=peer,
                        last_src=src, last_dst=dst, last_direction=direction
                    )
    except Exception as exc:
        with _traffic_lock:
            _traffic_state.update(status="unavailable",
                                  error=(str(exc) or exc.__class__.__name__)[:180],
                                  retry_at=time.monotonic() + 30.0,
                                  thread=None)


def _ensure_network_traffic_capture():
    if not ENABLE_LAN_DISCOVERY:
        return
    with _traffic_lock:
        current = _traffic_state.get("thread")
        if current is not None and current.is_alive():
            return
        if time.monotonic() < _traffic_state.get("retry_at", 0.0):
            return
    try:
        topology = network_topology(lan_inventory())
        interface = topology.get("interface")
        source_ip = topology.get("source_ip")
        if not interface:
            with _traffic_lock:
                _traffic_state.update(status="unavailable",
                                      error="No active network interface was detected.",
                                      retry_at=time.monotonic() + 30.0)
            return
        worker = threading.Thread(target=_traffic_capture_worker,
                                  args=(interface, source_ip), daemon=True,
                                  name="allerion-packet-capture")
        with _traffic_lock:
            _traffic_state.update(status="starting", interface=interface,
                                  source_ip=source_ip, error=None, thread=worker)
        worker.start()
    except Exception as exc:
        with _traffic_lock:
            _traffic_state.update(status="unavailable",
                                  error=(str(exc) or exc.__class__.__name__)[:180],
                                  retry_at=time.monotonic() + 30.0, thread=None)


def network_traffic_snapshot():
    if not ENABLE_LAN_DISCOVERY:
        raise PermissionError("Live network traffic requires ENABLE_LAN_DISCOVERY=1.")
    if platform.system() != "Linux" or not hasattr(socket, "AF_PACKET"):
        return {"available": False, "status": "unsupported",
                "error": "Live packet capture requires Linux AF_PACKET support."}
    _ensure_network_traffic_capture()
    now = time.time()
    with _traffic_lock:
        state = dict(_traffic_state)
        events = [event for event in _traffic_events if now - event[0] <= 3.0]
    peers = {}
    rx_bytes = tx_bytes = 0
    for at, peer, direction, size, src, dst in events:
        row = peers.setdefault(peer, {"ip": peer, "packets": 0,
                                      "rx_bytes": 0, "tx_bytes": 0,
                                      "last_seen": at})
        row["packets"] += 1
        row[direction + "_bytes"] += size
        row["last_seen"] = max(row["last_seen"], at)
        if direction == "rx":
            rx_bytes += size
        else:
            tx_bytes += size
    return {
        "available": state.get("status") == "active",
        "status": state.get("status"),
        "interface": state.get("interface"),
        "error": state.get("error"),
        "packet_count": state.get("packet_count", 0),
        "last_packet_age_ms": int(max(0, now - state["last_packet_at"]) * 1000)
                              if state.get("last_packet_at") else None,
        "active_peer": state.get("last_packet_peer"),
        "last_src": state.get("last_src"),
        "last_dst": state.get("last_dst"),
        "last_direction": state.get("last_direction"),
        "rx_bytes_per_sec": round(rx_bytes / 3.0, 1),
        "tx_bytes_per_sec": round(tx_bytes / 3.0, 1),
        "recent_peers": sorted(peers.values(),
                               key=lambda item: item["last_seen"], reverse=True)[:40],
        "scope": "IP packets visible on the monitoring server's selected network interface; switched traffic not delivered to this interface cannot be observed.",
    }


def network_diagnostics(target):
    try:
        addr = ipaddress.ip_address(target)
    except ValueError:
        raise ValueError("Target must be a private IP address; hostnames and public IPs are not allowed")
    if addr.version != 4 or not addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
        raise PermissionError("Only private LAN IP addresses are allowed")
    probes = []
    for port in (443, 80, 22, 8080):
        started = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.2)
        try:
            sock.connect((str(addr), port))
            probes.append({"port": port, "state": "open", "connect_ms": round((time.monotonic()-started)*1000, 1)})
        except (OSError, socket.timeout):
            probes.append({"port": port, "state": "closed_or_filtered", "connect_ms": None})
        finally:
            sock.close()
    return {"target": str(addr), "probes": probes, "tested_at": int(time.time()*1000),
            "packet_loss_pct": None, "note": "TCP connect checks only; packet loss is not measured by this probe."}


def energy_breakdown():
    if not METER:
        return {"days": [], "source": None}
    return {"days": [{"day": day, "sources": [{"source": source, "kwh": round(value, 4)}
                    for source, value in sorted(values.items())]}
                    for day, values in sorted(METER.source_hist.items())[-400:]],
            "source": "measured or estimated using the active power source; historical intervals retain their source label"}


def energy_forecast(days=30):
    if not METER:
        return {"available": False, "reason": "Energy collector is not initialized"}
    days = max(1, min(int(days), 90))
    cutoff = (datetime.now(TZ).date() - timedelta(days=30)).isoformat()
    values = [float(v) for day, v in METER.hist.items() if day >= cutoff and float(v) >= 0]
    if len(values) < 3:
        return {"available": False, "reason": "At least three recorded daily totals are required"}
    avg = sum(values) / len(values)
    return {"available": True, "days": days, "daily_average_kwh": round(avg, 3),
            "forecast_kwh": round(avg * days, 2),
            "estimated_cost": round(avg * days * float(CONFIG.get("price") or 0), 2),
            "currency": CONFIG.get("currency") or None, "method": "arithmetic mean of recorded daily totals",
            "observations": len(values), "coverage_start": cutoff, "source": "historical energy totals; forecast is an estimate"}


def integration_status():
    return {"items": [
        {"name": "webhook", "configured": bool(ALERT_WEBHOOK_URL), "enabled": bool(ALERT_WEBHOOK_URL)},
        {"name": "telegram", "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
         "enabled": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)},
    ]}


def test_alert_webhook():
    if not ALERT_WEBHOOK_URL:
        raise RuntimeError("Configure ALERT_WEBHOOK_URL on the backend first")
    parsed = urlparse(ALERT_WEBHOOK_URL)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("Webhook endpoint must use HTTPS")
    payload = json.dumps({"event": "allerion.integration_test", "message": "ALLERION webhook test",
                          "timestamp": int(time.time()*1000)}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "ALLERION/2"}
    if ALERT_WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = ALERT_WEBHOOK_SECRET
    req = urllib.request.Request(ALERT_WEBHOOK_URL, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=5) as response:
        return {"ok": 200 <= response.status < 300, "http_status": response.status}


def deliver_alert_event(event):
    payload = json.dumps(event).encode()
    if ALERT_WEBHOOK_URL:
        try:
            parsed = urlparse(ALERT_WEBHOOK_URL)
            if parsed.scheme == "https" and parsed.hostname:
                headers = {"Content-Type": "application/json", "User-Agent": "ALLERION/2"}
                if ALERT_WEBHOOK_SECRET:
                    headers["X-Webhook-Secret"] = ALERT_WEBHOOK_SECRET
                req = urllib.request.Request(ALERT_WEBHOOK_URL, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5) as response:
                    response.read(1024)
        except Exception:
            pass
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            text = ("ALLERION " + ("RECOVERED" if event.get("recovered") else "ALERT") +
                    " · " + str(event.get("server")) + " · " + str(event.get("metric")) +
                    " = " + str(event.get("value")) +
                    ("" if event.get("recovered") else " (threshold " + str(event.get("threshold")) + ")"))
            url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_BOT_TOKEN
            req = urllib.request.Request(url, data=json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode(),
                                        headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as response:
                response.read(1024)
        except Exception:
            pass


def evaluate_server_alerts(snapshot):
    if not ALERT_WEBHOOK_URL and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    host = str(snapshot.get("host") or socket.gethostname())
    metrics = [
        ("cpu", snapshot.get("cpu"), ALERT_THRESHOLDS["cpu"]),
        ("ram", snapshot.get("ram"), ALERT_THRESHOLDS["ram"]),
        ("temp", snapshot.get("temp"), ALERT_THRESHOLDS["temp"]),
        ("disk", max([float(x.get("pct", 0)) for x in snapshot.get("disks", [])] or [0]), ALERT_THRESHOLDS["disk"]),
    ]
    for metric, raw_value, threshold in metrics:
        if raw_value is None:
            continue
        active = float(raw_value) >= threshold
        previous = _alert_states.get(metric, False)
        if active == previous:
            continue
        _alert_states[metric] = active
        event = {"event": "allerion.metric_alert", "server": host, "metric": metric,
                 "value": round(float(raw_value), 1), "threshold": threshold,
                 "recovered": not active, "timestamp": int(time.time()*1000)}
        threading.Thread(target=deliver_alert_event, args=(event,), daemon=True).start()


def test_telegram():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on the backend first")
    url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_BOT_TOKEN
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": "ALLERION Telegram integration test"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as response:
        data = json.loads(response.read(100_000).decode())
    if not data.get("ok"):
        raise RuntimeError("Telegram API did not accept the test message")
    return {"ok": True, "delivered": True}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authed(self, u):
        # Header-only auth avoids leaking credentials into URL logs, browser history and referrers.
        given = self.headers.get("X-Token", "")
        return bool(TOKEN) and given == TOKEN

    def do_GET(self):
        u = urlparse(self.path)
        if u.path =="/health":
            self.send(200, '{"status":"ok"}', "application/json")
            return
        if u.path == "/":
            self.send(200, PAGE, "text/html; charset=utf-8")
            return
        if u.path == "/manifest.json":
            self.send(200, json.dumps(MANIFEST), "application/manifest+json")
            return
        if u.path == "/icon.svg":
            self.send(200, ICON, "image/svg+xml")
            return
        if not self.authed(u):
            self.send(401, '{"error":"unauthorized"}', "application/json")
            return
        try:
            if u.path == "/api/speedtest/servers":
                payload = list_ookla_servers()
            elif u.path == "/api/stats":
                payload = stats
            elif u.path == "/api/history":
                payload = HIST.get(parse_qs(u.query).get("range", ["1h"])[0])
            elif u.path == "/api/energy":
                days = sorted(METER.hist.items()) if METER else []
                payload = {"days": [{"day": d, "kwh": round(v, 3)} for d, v in days]}
            elif u.path == "/api/power/config":
                payload = dict(CONFIG)
                payload["has_key"] = bool(payload.get("tuya_key"))
                payload["tuya_key"] = ""
                payload["has_pass"] = bool(payload.get("tapo_pass"))
                payload["tapo_pass"] = ""
            elif u.path == "/api/capabilities":
                payload = api_capabilities()
            elif u.path == "/api/processes":
                payload = process_inventory(parse_qs(u.query).get("limit", ["30"])[0])
            elif u.path == "/api/containers":
                payload = container_inventory()
            elif u.path.startswith("/api/containers/") and u.path.endswith("/stats"):
                container_id = u.path[len("/api/containers/"):-len("/stats")].strip("/")
                payload = container_stats(container_id)
            elif u.path == "/api/services":
                payload = service_inventory()
            elif u.path == "/api/network/devices":
                payload = lan_inventory()
            elif u.path == "/api/network/topology":
                payload = network_topology(lan_inventory())
            elif u.path == "/api/network/traffic":
                payload = network_traffic_snapshot()
            elif u.path == "/api/network/diagnostics":
                target = parse_qs(u.query).get("target", [""])[0]
                payload = network_diagnostics(target)
            elif u.path == "/api/energy/breakdown":
                payload = energy_breakdown()
            elif u.path == "/api/energy/forecast":
                payload = energy_forecast(parse_qs(u.query).get("days", ["30"])[0])
            elif u.path == "/api/integrations":
                payload = integration_status()
            else:
                self.send(404, "Not found", "text/plain")
                return
            self.send(200, json.dumps(payload, default=str), "application/json")
        except PermissionError as exc:
            self.send(403, json.dumps({"error": str(exc), "code": "feature_disabled_or_forbidden"}), "application/json")
        except (ValueError, TypeError) as exc:
            self.send(400, json.dumps({"error": str(exc), "code": "bad_request"}), "application/json")
        except FileNotFoundError as exc:
            self.send(501, json.dumps({"error": str(exc), "code": "not_configured"}), "application/json")
        except Exception as exc:
            self.send(503, json.dumps({"error": (str(exc) or exc.__class__.__name__)[:200],
                                       "code": "collector_unavailable"}), "application/json")

    def do_POST(self):
        u = urlparse(self.path)
        if not self.authed(u):
            return self.send(401, '{"error":"unauthorized"}', "application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = clean_cfg(json.loads(self.rfile.read(n) or b"{}"))
        except ValueError:
            return self.send(400, '{"error":"bad request"}', "application/json")
        for secret in ("tuya_key", "tapo_pass"):
            if body.get(secret) == "":
                body.pop(secret)            # empty means keep the saved secret
        if u.path == "/api/power/config":
            CONFIG.update(body)
            _plug.update(t=0.0, w=None, v=None, err=None)
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                write_private_json(CFG_FILE, CONFIG)
                saved = True
            except OSError:
                saved = False
            self.send(200, json.dumps({"ok": True, "saved": saved}), "application/json")
        elif u.path == "/api/speedtest":
            server_id = body.get("server_id")
            if server_id is not None and not re.fullmatch(r"\d{1,10}", str(server_id)):
                self.send(400, json.dumps({"ok": False, "error": "Invalid Ookla server ID"}), "application/json")
                return
            threading.Thread(target=run_speedtest, args=(server_id,), daemon=True).start()
            self.send(200, '{"ok":true}', "application/json")
        elif u.path == "/api/power/test":
            cand = {**CONFIG, **body}
            try:
                if cand["source"] == "cpu":
                    out = {"ok": True, "watts": None, "note": "Using the CPU reading plus your base watts."}
                else:
                    w, v, raw = read_plug(cand)
                    out = {"ok": True, "watts": round(w, 1), "voltage": v, "raw": json.dumps(raw)[:300]}
            except Exception as e:
                out = {"ok": False, "error": (str(e) or e.__class__.__name__)[:300]}
            self.send(200, json.dumps(out), "application/json")
        elif u.path == "/api/integrations/webhook/test":
            try:
                out = test_alert_webhook()
                self.send(200, json.dumps(out), "application/json")
            except Exception as e:
                self.send(502, json.dumps({"ok": False, "error": "Webhook test failed: " + e.__class__.__name__}), "application/json")
        elif u.path == "/api/integrations/telegram/test":
            try:
                out = test_telegram()
                self.send(200, json.dumps(out), "application/json")
            except Exception as e:
                self.send(502, json.dumps({"ok": False, "error": "Telegram test failed: " + e.__class__.__name__}), "application/json")
        else:
            self.send(404, "Not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=collector, daemon=True).start()
    # Automatic speed tests remain disabled; manual tests are available in the app.
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = "<server-ip>"
    print(f"Monitor running: http://{ip}:{PORT}  (auth {'on' if TOKEN else 'OFF - set TOKEN'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
