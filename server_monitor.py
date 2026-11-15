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
import jellyfin_analytics
import fcm_notifications
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


def _block_parent(sys_path):
    name = os.path.basename(sys_path.rstrip("/"))
    if os.path.exists(os.path.join(sys_path, "partition")):
        return os.path.basename(os.path.dirname(sys_path.rstrip("/")))
    return name


def _mounted_backing_disks(major_minor, visited=None):
    visited = visited or set()
    if major_minor in visited:
        return set()
    visited.add(major_minor)
    link = os.path.join("/hostfs/sys/dev/block", major_minor)
    if not os.path.exists(link):
        return set()
    sys_path = os.path.realpath(link)
    slaves = glob.glob(os.path.join(sys_path, "slaves", "*"))
    if slaves:
        result = set()
        for slave in slaves:
            dev_file = os.path.join(slave, "dev")
            try:
                with open(dev_file, encoding="ascii") as handle:
                    result.update(_mounted_backing_disks(handle.read().strip(), visited))
            except OSError:
                continue
        return result
    if "/virtual/" in sys_path:
        return set()
    return {_block_parent(sys_path)}


def _host_disk_identity(major_minor, source):
    sys_link = os.path.join("/hostfs/sys/dev/block", major_minor)
    try:
        sys_path = os.path.realpath(sys_link)
        if not os.path.exists(sys_link):
            raise OSError("sysfs block device unavailable")
        name = _block_parent(sys_path)
        removable = False
        removable_file = os.path.join("/hostfs/sys/class/block", os.path.basename(sys_path), "removable")
        try:
            with open(removable_file, encoding="ascii") as handle:
                removable = handle.read().strip() == "1"
        except OSError:
            pass
        usb = "/usb" in sys_path.lower()
        model = ""
        model_file = os.path.join("/hostfs/sys/class/block", name, "device", "model")
        try:
            with open(model_file, encoding="utf-8") as handle:
                model = handle.read().strip()
        except OSError:
            pass
        return name, sys_path, removable or usb, "USB" if usb else "Internal", model
    except OSError:
        return source, "", False, "Unknown", ""


def _unmounted_physical_disks(mounted_names):
    found = []
    excluded_prefixes = ("loop", "ram", "zram", "dm-", "md", "nbd", "sr", "fd")
    for block_path in glob.glob("/hostfs/sys/class/block/*"):
        name = os.path.basename(block_path)
        if name.startswith(excluded_prefixes):
            continue
        try:
            sys_path = os.path.realpath(block_path)
            if "/virtual/" in sys_path or os.path.exists(os.path.join(sys_path, "partition")):
                continue
            with open(os.path.join(block_path, "size"), encoding="ascii") as handle:
                size_bytes = int(handle.read().strip()) * 512
            with open(os.path.join(block_path, "dev"), encoding="ascii") as handle:
                major_minor = handle.read().strip()
            if size_bytes <= 0 or name in mounted_names:
                continue
            removable = False
            try:
                with open(os.path.join(block_path, "removable"), encoding="ascii") as handle:
                    removable = handle.read().strip() == "1"
            except OSError:
                pass
            usb = "/usb" in sys_path.lower()
            model = ""
            try:
                with open(os.path.join(block_path, "device", "model"), encoding="utf-8") as handle:
                    model = handle.read().strip()
            except OSError:
                pass
            found.append({
                "id": major_minor, "mount": "Not mounted", "pct": None,
                "used_gb": 0.0, "total_gb": round(size_bytes / 1e9, 1), "free_gb": None,
                "device": "/dev/" + name, "disk": model or name, "model": model,
                "filesystem": "", "disk_id": name, "removable": removable or usb,
                "connection": "USB" if usb else "Internal", "mounted": False,
            })
        except (OSError, ValueError):
            continue
    return found


def disks():
    out = []
    seen = set()
    mounted_names = set()
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
            if filesystem in _PSEUDO_FILESYSTEMS or not source.startswith("/dev/"):
                continue
            identity = (major_minor, _unescape_mount_field(fields[3]))
            if identity in seen:
                continue
            host_path = os.path.normpath(os.path.join(_HOST_ROOT, mountpoint.lstrip("/")))
            if not os.path.exists(host_path):
                continue
            if mountpoint != "/" and not os.path.ismount(host_path):
                continue
            usage = psutil.disk_usage(host_path)
            disk_name, sys_path, removable, connection, model = _host_disk_identity(major_minor, source)
            seen.add(identity)
            mounted_names.update(_mounted_backing_disks(major_minor))
            if sys_path and "/virtual/" not in sys_path:
                mounted_names.add(disk_name)
            out.append({
                "id": major_minor, "mount": mountpoint, "pct": usage.percent,
                "used_gb": round(usage.used / 1e9, 1), "total_gb": round(usage.total / 1e9, 1),
                "free_gb": round(usage.free / 1e9, 1), "device": source,
                "disk": model or disk_name, "model": model, "filesystem": filesystem,
                "disk_id": disk_name, "removable": removable, "connection": connection,
                "mounted": True,
            })
        except (OSError, ValueError, IndexError):
            continue

    if not out:
        for part in psutil.disk_partitions(all=False):
            if not part.device.startswith("/dev/") or part.fstype in _PSEUDO_FILESYSTEMS:
                continue
            path = part.mountpoint
            if not path.startswith(_HOST_ROOT):
                path = os.path.normpath(os.path.join(_HOST_ROOT, path.lstrip("/")))
            try:
                usage = psutil.disk_usage(path)
                disk_name, _, removable, connection, model = _host_disk_identity("0:0", part.device)
                out.append({
                    "id": part.device, "mount": part.mountpoint, "pct": usage.percent,
                    "used_gb": round(usage.used / 1e9, 1), "total_gb": round(usage.total / 1e9, 1),
                    "free_gb": round(usage.free / 1e9, 1), "device": part.device,
                    "disk": model or disk_name, "model": model, "filesystem": part.fstype,
                    "disk_id": disk_name, "removable": removable, "connection": connection,
                    "mounted": True,
                })
            except OSError:
                continue
    out.extend(_unmounted_physical_disks(mounted_names))
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
        mounted_disks = [x for x in dk if x.get("mounted", True)]
        dtot = sum(x["total_gb"] for x in mounted_disks)
        HIST.add({"t": now, "cpu": stats["cpu"], "ram": vm.percent, "temp": stats["temp"], "down": down * 8, "up": up * 8,
                  "power": power["watts"], "gpu": stats["gpu"],
                  "disk": (sum(x["used_gb"] for x in mounted_disks) / dtot * 100) if dtot else None})
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

/* Allerion Android "Obsidian Signal" visual system */
:root{color-scheme:dark;--bg:#090B10;--fg:#F3F5F8;--mut:#A0A8B5;--line:#252A34;--ok:#43D6A0;--warn:#FFC46B;--bad:#FF3548;--red:#FF3548;--deep:#9E1B2A;--panel:#11151D;--panel2:#151A23}
*{box-sizing:border-box}body{background:radial-gradient(ellipse at 80% -10%,#35121b 0,transparent 36%),var(--bg);color:var(--fg);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh}
main{max-width:1180px;margin:0 auto;padding:28px 28px 54px}header{align-items:center!important;margin-bottom:24px!important;padding-bottom:18px;border-bottom:1px solid var(--line)}
header h1{font-size:19px;letter-spacing:.19em;font-weight:800;text-transform:uppercase}header:before{content:"";display:block;width:46px;height:46px;flex:0 0 46px;margin-right:12px;background:url('/icon.svg') center/contain no-repeat}header .sub{font-size:11px;letter-spacing:.12em;text-transform:uppercase}
section{background:linear-gradient(145deg,#151a23,#10141b);border:1px solid var(--line);border-radius:15px;padding:18px;margin:0 0 14px;box-shadow:0 14px 35px #0004;border-top:1px solid var(--line)}
.lab{color:var(--mut);font-size:11px;letter-spacing:.09em;text-transform:uppercase;font-weight:650}.big{font-size:2.15rem;font-weight:720;letter-spacing:-.045em;color:var(--fg)!important}.big small{font-size:.85rem;color:var(--mut)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}svg polyline{stroke:var(--red);stroke-width:2;opacity:.95}.bar{background:#252a34;height:7px;border-radius:9px}.bar i{background:var(--red);border-radius:9px}
.two{gap:22px}.mid{font-size:1.4rem;font-weight:700;letter-spacing:-.03em}.sub{color:var(--mut)}#msg{background:#3b1d25;border:1px solid #6a2a36;border-radius:12px;color:#ff8993;padding:14px}
@media(min-width:760px){main{padding-top:34px}#app{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}#app>section{min-width:0}#app>section:first-child{grid-column:span 2}}
@media(max-width:560px){main{padding:16px 14px 34px}section{padding:15px;border-radius:13px}.big{font-size:1.9rem}header h1{font-size:16px}}
</style></head><body><main>
<header><h1 id="host">ALLERION</h1><span class="sub" id="up">SERVER INTELLIGENCE</span></header>
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

MANIFEST = {"name":"ALLERION","short_name":"ALLERION","start_url":"/","display":"standalone","background_color":"#090B10","theme_color":"#090B10","icons":[{"src":"/icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any"}]}
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 448 448"><rect width="448" height="448" rx="76" fill="#090B10"/><image width="448" height="448" href="data:image/png;base64,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"/></svg>')


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
ALERT_STATE_FILE = os.path.join(DATA_DIR, "alert_states.json")
_ALERT_STATE_LOCK = threading.RLock()
_ALERT_RETRY_SECONDS = 60
_alert_pending = set()
_alert_retry_after = {}


def _load_alert_states():
    try:
        with open(ALERT_STATE_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        return {str(key): bool(value) for key, value in saved.items()} if isinstance(saved, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _persist_alert_states_locked():
    os.makedirs(DATA_DIR, exist_ok=True)
    temporary = ALERT_STATE_FILE + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(_alert_states, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, ALERT_STATE_FILE)
    os.chmod(ALERT_STATE_FILE, 0o600)


_alert_states = _load_alert_states()
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
            "network_quality": ENABLE_LAN_DISCOVERY and platform.system() == "Linux",
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


def mac_vendor(mac):
    """Best-effort vendor hint from a small built-in OUI map; not authoritative."""
    value = str(mac or "").lower()
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", value):
        return None
    try:
        first = int(value[:2], 16)
    except ValueError:
        return None
    if first & 2:
        return "Private / randomized MAC"
    oui = value[:8]
    vendors = {
        "00:1a:11": "Google", "3c:5a:b4": "Google", "f4:f5:d8": "Google",
        "3c:22:fb": "Apple", "a4:83:e7": "Apple", "f0:18:98": "Apple",
        "00:16:cb": "Apple", "ac:de:48": "Apple",
        "8c:85:90": "Samsung", "cc:07:ab": "Samsung", "e4:7c:f9": "Samsung",
        "28:6c:07": "Huawei", "48:db:50": "Huawei", "ac:e2:d3": "Huawei",
        "00:e0:fc": "Huawei", "f8:e8:11": "Huawei",
        "64:09:80": "Xiaomi", "78:11:dc": "Xiaomi", "c8:ff:28": "Xiaomi",
        "50:64:2b": "TP-Link", "c0:06:c3": "TP-Link", "ec:08:6b": "TP-Link",
        "b0:be:76": "TP-Link", "f4:ec:38": "TP-Link",
        "00:1d:aa": "ASUSTek", "2c:56:dc": "ASUSTek", "ac:9e:17": "ASUSTek",
        "00:1f:3b": "Intel", "3c:97:0e": "Intel", "a4:4c:c8": "Intel",
        "00:25:90": "Supermicro", "b8:ae:ed": "Dell", "f8:bc:12": "Dell",
        "00:1e:68": "Hewlett Packard", "3c:d9:2b": "Hewlett Packard",
        "00:0c:29": "VMware", "00:50:56": "VMware",
        "dc:a6:32": "Raspberry Pi", "e4:5f:01": "Raspberry Pi",
        "b8:27:eb": "Raspberry Pi",
    }
    return vendors.get(oui, "Unknown vendor")


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
            if len(cols) < 2 or not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", cols[0]):
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

    for item in found.values():
        item["vendor"] = mac_vendor(item.get("mac"))

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

def network_dns_config():
    """Read DNS resolvers configured on the CasaOS host, not Docker synthetic DNS."""
    candidates = [
        ("/hostfs/run/systemd/resolve/resolv.conf", "Host systemd-resolved upstream"),
        ("/hostfs/run/NetworkManager/no-stub-resolv.conf", "Host NetworkManager upstream"),
        ("/hostfs/etc/resolv.conf", "Host resolv.conf"),
        ("/run/systemd/resolve/resolv.conf", "systemd-resolved upstream"),
        ("/etc/resolv.conf", "Process resolv.conf"),
    ]
    local_stubs = {"127.0.0.53", "127.0.0.1", "127.0.0.11", "::1"}
    seen = set()
    servers = []
    selected_source = None
    search_domains = []
    for path, source in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except (OSError, UnicodeError):
            continue
        parsed = []
        domains = []
        for line in lines:
            fields = line.split("#", 1)[0].split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                address = fields[1].strip()
                try:
                    ipaddress.ip_address(address)
                except ValueError:
                    continue
                parsed.append(address)
            elif fields and fields[0] in ("search", "domain"):
                domains.extend(fields[1:])
        if not parsed:
            continue
        non_stub = [ip for ip in parsed if ip not in local_stubs]
        if non_stub:
            selected_source = source
            search_domains = domains
            for address in non_stub:
                if address not in seen:
                    servers.append({"address": address, "type": "upstream", "source": source})
                    seen.add(address)
            break
        if not servers:
            selected_source = source
            search_domains = domains
            for address in parsed:
                if address not in seen:
                    servers.append({"address": address, "type": "local_stub", "source": source})
                    seen.add(address)
    return {
        "servers": servers,
        "count": len(servers),
        "source": selected_source,
        "search_domains": search_domains,
        "detected": bool(servers),
        "note": "DNS configuration read from CasaOS host resolver files. This is configured host DNS, not proof every LAN client uses the same resolver.",
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
        "dns": network_dns_config(),
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
        raise PermissionError('Live network traffic requires ENABLE_LAN_DISCOVERY=1.')
    if platform.system() != 'Linux' or not hasattr(socket, 'AF_PACKET'):
        return {'available': False, 'status': 'unsupported', 'error': 'Live packet capture requires Linux AF_PACKET support.'}
    _ensure_network_traffic_capture()
    now = time.time()
    with _traffic_lock:
        state = dict(_traffic_state)
        events = [event for event in _traffic_events if now - event[0] <= 3.0]
    peers = {}
    rx_bytes = tx_bytes = 0
    for at, peer, direction, size, src, dst in events:
        row = peers.setdefault(peer, {'ip': peer, 'packets': 0, 'rx_bytes': 0, 'tx_bytes': 0, 'last_seen': at})
        row['packets'] += 1
        row[direction + '_bytes'] += size
        row['last_seen'] = max(row['last_seen'], at)
        if direction == 'rx': rx_bytes += size
        else: tx_bytes += size
    payload = {'available': state.get('status') == 'active', 'status': state.get('status'),
        'interface': state.get('interface'), 'error': state.get('error'),
        'packet_count': state.get('packet_count', 0),
        'last_packet_age_ms': int(max(0, now - state['last_packet_at']) * 1000) if state.get('last_packet_at') else None,
        'active_peer': state.get('last_packet_peer'), 'last_src': state.get('last_src'),
        'last_dst': state.get('last_dst'), 'last_direction': state.get('last_direction'),
        'rx_bytes_per_sec': round(rx_bytes / 3.0, 1), 'tx_bytes_per_sec': round(tx_bytes / 3.0, 1),
        'recent_peers': sorted(peers.values(), key=lambda item: item['last_seen'], reverse=True)[:40],
        'scope': "IP packets visible on the monitoring server's selected network interface; switched traffic not delivered to this interface cannot be observed."}
    _record_network_intelligence(payload)
    return payload

_NETWORK_INTEL_FILE = os.path.join(DATA_DIR, 'network_intelligence.json')
_network_intel_lock = threading.Lock()
_network_intel = {'samples': [], 'devices': {}, 'alerts': []}
try:
    with open(_NETWORK_INTEL_FILE, encoding='utf-8') as _handle:
        _saved = json.load(_handle)
    if isinstance(_saved, dict): _network_intel.update(_saved)
except (OSError, ValueError, TypeError):
    pass

def _record_network_intelligence(payload):
    now = int(time.time())
    with _network_intel_lock:
        samples = _network_intel.setdefault('samples', [])
        sample = {'ts': now, 'rx_bytes_per_sec': payload.get('rx_bytes_per_sec', 0), 'tx_bytes_per_sec': payload.get('tx_bytes_per_sec', 0), 'packet_count': payload.get('packet_count', 0), 'active_peers': len(payload.get('recent_peers', []))}
        if not samples or now - int(samples[-1].get('ts', 0)) >= 15: samples.append(sample)
        cutoff = now - 604800
        _network_intel['samples'] = [x for x in samples if int(x.get('ts', 0)) >= cutoff][-3000:]
        devices = _network_intel.setdefault('devices', {})
        had_inventory = bool(devices)
        new_device_alerts = []
        try:
            inventory = lan_inventory()
            for device in inventory.get('items', []):
                ip = str(device.get('ip') or '')
                mac = str(device.get('mac') or '').lower()
                if not ip: continue
                key = mac or ip
                old = devices.get(key, {})
                if had_inventory and not old:
                    new_device_alerts.append({'ts': now, 'type': 'new_device', 'severity': 'notice', 'ip': ip, 'mac': mac or None, 'vendor': device.get('vendor'), 'message': 'A previously unseen device appeared in the passive neighbor cache.'})
                devices[key] = dict(old, ip=ip, mac=mac or None, vendor=device.get('vendor') or old.get('vendor'), hostname=device.get('hostname') or old.get('hostname'), interface=device.get('interface') or old.get('interface'), state=device.get('state') or old.get('state'), first_seen=old.get('first_seen', now), last_seen=now, identity_source='passive ARP/neighbour cache')
        except Exception: pass
        alerts = _network_intel.setdefault('alerts', [])
        alerts.extend(new_device_alerts)
        previous = _network_intel['samples'][:-1][-20:]
        if len(previous) >= 5:
            rates = sorted(max(float(x.get('rx_bytes_per_sec', 0)), float(x.get('tx_bytes_per_sec', 0))) for x in previous)
            baseline = rates[len(rates) // 2]
            current = max(float(sample['rx_bytes_per_sec']), float(sample['tx_bytes_per_sec']))
            last_spike = next((item for item in reversed(alerts) if item.get('type') == 'traffic_spike'), None)
            if current >= 1000000 and current > max(baseline * 10, 5000000) and (not last_spike or now - int(last_spike.get('ts', 0)) > 300):
                alerts.append({'ts': now, 'type': 'traffic_spike', 'severity': 'notice', 'rate_bytes_per_sec': round(current, 1), 'baseline_bytes_per_sec': round(baseline, 1), 'message': 'Traffic rate is substantially above its recent baseline.'})
        _network_intel['alerts'] = alerts[-100:]
        try: write_private_json(_NETWORK_INTEL_FILE, _network_intel)
        except OSError: pass

def network_intelligence_history():
    with _network_intel_lock:
        return {'retention_days': 7, 'sample_interval_seconds': 15, 'samples': list(_network_intel.get('samples', [])), 'devices': sorted(list(_network_intel.get('devices', {}).values()), key=lambda x: x.get('last_seen', 0), reverse=True)[:250], 'alerts': list(_network_intel.get('alerts', []))[-100:], 'note': 'Metadata only; device visibility depends on passive neighbour-cache observations and traffic is limited to packets visible on the monitoring interface.'}

def network_quality():
    """Measure ICMP latency and packet loss to the CasaOS default gateway and a public reachability target."""
    if not ENABLE_LAN_DISCOVERY:
        raise PermissionError("Network quality checks require ENABLE_LAN_DISCOVERY=1.")

    gateway = None
    try:
        route = subprocess.run(["ip", "-4", "route", "show", "default"],
                               capture_output=True, text=True, timeout=2)
        for line in route.stdout.splitlines():
            columns = line.split()
            if columns and columns[0] == "default" and "via" in columns:
                index = columns.index("via")
                if index + 1 < len(columns):
                    gateway = columns[index + 1]
                    break
    except (OSError, subprocess.TimeoutExpired):
        pass

    targets = []
    if gateway:
        targets.append({"name": "Default gateway", "target": gateway})
    targets.append({"name": "Internet reachability", "target": "1.1.1.1"})

    results = []
    for target in targets:
        item = {**target, "status": "unavailable", "latency_ms": None,
                "packet_loss_pct": None, "samples": 3}
        try:
            probe = subprocess.run(
                ["ping", "-n", "-c", "3", "-W", "1", target["target"]],
                capture_output=True, text=True, timeout=6
            )
            output = (probe.stdout or "") + "\n" + (probe.stderr or "")
            loss = re.search(r"(\d+(?:\.\d+)?)%\s*packet loss", output)
            latency = re.search(
                r"(?:rtt|round-trip) min/avg/max(?:/(?:mdev|stddev))? = [\d.]+/([\d.]+)/",
                output
            )
            if loss:
                item["packet_loss_pct"] = float(loss.group(1))
                item["latency_ms"] = float(latency.group(1)) if latency else None
                item["status"] = "ok" if item["packet_loss_pct"] < 100 else "unreachable"
            else:
                item["error"] = "The ping utility did not return parseable packet statistics."
        except FileNotFoundError:
            item["error"] = "The ping utility is not installed in the monitoring environment."
        except subprocess.TimeoutExpired:
            item["error"] = "The ICMP probe timed out before statistics were returned."
        except OSError as exc:
            item["error"] = "ICMP probe unavailable: " + exc.__class__.__name__
        results.append(item)

    return {
        "available": any(item["packet_loss_pct"] is not None for item in results),
        "method": "ICMP echo, 3 probes per target",
        "targets": results,
        "note": "ICMP may be filtered by the router or destination. Unavailable probes do not by themselves prove an outage.",
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
        {"name": "fcm", "configured": fcm_notifications.configured(),
         "enabled": fcm_notifications.configured() and fcm_notifications.registered_count() > 0,
         "devices": fcm_notifications.registered_count()},
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
    """Deliver to every configured channel; return True only when all configured sends succeed."""
    attempted = False
    all_ok = True
    payload = json.dumps(event).encode()
    if ALERT_WEBHOOK_URL:
        attempted = True
        try:
            parsed = urlparse(ALERT_WEBHOOK_URL)
            if parsed.scheme != "https" or not parsed.hostname:
                raise RuntimeError("Webhook endpoint must use HTTPS")
            headers = {"Content-Type": "application/json", "User-Agent": "ALLERION/2"}
            if ALERT_WEBHOOK_SECRET:
                headers["X-Webhook-Secret"] = ALERT_WEBHOOK_SECRET
            req = urllib.request.Request(ALERT_WEBHOOK_URL, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("Webhook returned HTTP " + str(response.status))
        except Exception as exc:
            all_ok = False
            print("[alerts] webhook delivery failed: " + exc.__class__.__name__, flush=True)
    if fcm_notifications.configured() and fcm_notifications.registered_count():
        attempted = True
        try:
            result = fcm_notifications.send_alert(event)
            if not result.get("ok") or result.get("sent", 0) < 1:
                raise RuntimeError("FCM did not confirm delivery to a registered device")
        except Exception as exc:
            all_ok = False
            print("[alerts] FCM delivery failed: " + exc.__class__.__name__, flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        attempted = True
        try:
            text = ("ALLERION " + ("RECOVERED" if event.get("recovered") else "ALERT") +
                    " · " + str(event.get("server")) + " · " + str(event.get("metric")) +
                    " = " + str(event.get("value")) +
                    ("" if event.get("recovered") else " (threshold " + str(event.get("threshold")) + ")"))
            url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_BOT_TOKEN
            req = urllib.request.Request(url, data=json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode(),
                                        headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read(100_000).decode())
            if not result.get("ok"):
                raise RuntimeError("Telegram did not accept the alert")
        except Exception as exc:
            all_ok = False
            print("[alerts] Telegram delivery failed: " + exc.__class__.__name__, flush=True)
    return attempted and all_ok


def _deliver_and_commit_alert(event, active):
    metric = event["metric"]
    delivered = False
    try:
        delivered = deliver_alert_event(event)
        if delivered:
            with _ALERT_STATE_LOCK:
                had_previous = metric in _alert_states
                previous = _alert_states.get(metric)
                _alert_states[metric] = bool(active)
                try:
                    _persist_alert_states_locked()
                except Exception:
                    if had_previous:
                        _alert_states[metric] = previous
                    else:
                        _alert_states.pop(metric, None)
                    raise
                _alert_retry_after.pop(metric, None)
        else:
            print("[alerts] delivery not confirmed; will retry " + metric, flush=True)
    except Exception as exc:
        delivered = False
        print("[alerts] delivery/persistence failed: " + exc.__class__.__name__, flush=True)
    finally:
        with _ALERT_STATE_LOCK:
            _alert_pending.discard(metric)
            if not delivered:
                _alert_retry_after[metric] = time.time() + _ALERT_RETRY_SECONDS


def evaluate_server_alerts(snapshot):
    if (not ALERT_WEBHOOK_URL and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
            and not (fcm_notifications.configured() and fcm_notifications.registered_count())):
        return
    host = str(snapshot.get("host") or socket.gethostname())
    metrics = [
        ("cpu", snapshot.get("cpu"), ALERT_THRESHOLDS["cpu"]),
        ("ram", snapshot.get("ram"), ALERT_THRESHOLDS["ram"]),
        ("temp", snapshot.get("temp"), ALERT_THRESHOLDS["temp"]),
        ("disk", max([float(x.get("pct") or 0) for x in snapshot.get("disks", []) if x.get("mounted", True)] or [0]), ALERT_THRESHOLDS["disk"]),
    ]
    now = time.time()
    for metric, raw_value, threshold in metrics:
        if raw_value is None:
            continue
        active = float(raw_value) >= threshold
        with _ALERT_STATE_LOCK:
            previous = _alert_states.get(metric, False)
            if active == previous or metric in _alert_pending:
                continue
            if now < _alert_retry_after.get(metric, 0):
                continue
            _alert_pending.add(metric)
        event = {"event": "allerion.metric_alert", "server": host, "metric": metric,
                 "value": round(float(raw_value), 1), "threshold": threshold,
                 "recovered": not active, "timestamp": int(time.time()*1000)}
        threading.Thread(target=_deliver_and_commit_alert, args=(event, active), daemon=True).start()


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
            try:
                with open(os.path.join(os.path.dirname(__file__), "console.html"), encoding="utf-8") as handle:
                    self.send(200, handle.read(), "text/html; charset=utf-8")
            except OSError:
                self.send(503, "Allerion Console UI is not installed", "text/plain; charset=utf-8")
            return
        if u.path == "/monitor":
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
            if u.path == "/api/jellyfin/analytics":
                period = parse_qs(u.query).get("period", ["week"])[0].lower()
                if period not in ("day", "week", "month", "year"):
                    raise ValueError("period must be day, week, month or year")
                payload = jellyfin_analytics.report(period)
            elif u.path == "/api/speedtest/servers":
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
            elif u.path == "/api/network/intelligence/history":
                payload = network_intelligence_history()
            elif u.path == "/api/network/quality":
                payload = network_quality()
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
            raw_body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(raw_body, dict):
                raise ValueError("JSON object required")
        except (ValueError, TypeError):
            return self.send(400, '{"error":"bad request"}', "application/json")
        if u.path == "/api/notifications/fcm/register":
            try:
                out = fcm_notifications.register_device(raw_body.get("token"))
                return self.send(200, json.dumps(out), "application/json")
            except ValueError as exc:
                return self.send(400, json.dumps({"ok": False, "error": str(exc)}), "application/json")
            except Exception as exc:
                return self.send(503, json.dumps({"ok": False, "error": str(exc)[:180]}), "application/json")
        elif u.path == "/api/notifications/fcm/unregister":
            try:
                out = fcm_notifications.unregister_device(raw_body.get("token"))
                return self.send(200, json.dumps(out), "application/json")
            except ValueError as exc:
                return self.send(400, json.dumps({"ok": False, "error": str(exc)}), "application/json")
        body = clean_cfg(raw_body)
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
        elif u.path == "/api/integrations/fcm/test":
            try:
                out = fcm_notifications.send_test()
                self.send(200, json.dumps(out), "application/json")
            except Exception as e:
                self.send(502, json.dumps({"ok": False, "error": "FCM test failed: " + str(e)[:180]}), "application/json")
        elif u.path == "/api/integrations/fcm/simulate-alert":
            try:
                if not fcm_notifications.configured() or fcm_notifications.registered_count() == 0:
                    raise RuntimeError("FCM is not configured or no devices are registered")
                event = {"event": "allerion.metric_alert", "server": socket.gethostname(),
                         "metric": "cpu", "value": 95.0, "threshold": 90.0,
                         "recovered": False, "simulated": True, "timestamp": int(time.time()*1000)}
                out = fcm_notifications.send_alert(event)
                if not out.get("ok") or out.get("sent", 0) < 1:
                    raise RuntimeError("FCM did not confirm delivery to a registered device")
                self.send(200, json.dumps({"ok": True, "simulated": True, **out}), "application/json")
            except Exception as e:
                self.send(502, json.dumps({"ok": False, "error": "Simulated alert failed: " + str(e)[:180]}), "application/json")
        else:
            self.send(404, "Not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=collector, daemon=True).start()
    jellyfin_analytics.start()
    # Automatic speed tests remain disabled; manual tests are available in the app.
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = "<server-ip>"
    print(f"Monitor running: http://{ip}:{PORT}  (auth {'on' if TOKEN else 'OFF - set TOKEN'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
