#!/usr/bin/env python3
"""
Scale Push Monitor - receive Web API "Push" messages from a weighing scale
display and show them on a live web dashboard.

Zero third-party dependencies: Python 3.8+ standard library only.

    python scale_push_monitor.py            # listen on 0.0.0.0:8080
    python scale_push_monitor.py --port 9000
    python scale_push_monitor.py --demo     # also generate fake pushes + a mock scale API
    python scale_push_monitor.py --scale-ip 192.168.1.141 --scale-port 8080

Program the scale to push to:  http://<this-PC-LAN-IP>:<port>/weight
Open the dashboard at:         http://localhost:<port>/
The dashboard can also send ZERO / TARE / UNIT / NET-GROSS commands to the
scale's HTTP API (http://<scale-ip>:<scale-port>/api/1.0/...).
"""

import argparse
import collections
import datetime
import json
import queue
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1"
LOG_SIZE = 50
STALE_SECONDS = 3.0


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "on", "gross")
    return None


def to_float(v):
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


def auto_decimals(v):
    """Decimals needed for a value the scale most likely sent as a 32-bit float.

    64.9000015258789 is the float32 value 64.9 widened to double, so we pick
    the shortest decimal that maps back to the same float32.
    """
    if v is None:
        return 0
    try:
        target = struct.pack("<f", v)
        for p in range(0, 5):
            if struct.pack("<f", round(v, p)) == target:
                return p
    except (OverflowError, struct.error):
        pass
    return 4


def fmt(v, dp):
    return None if v is None else "%.*f" % (dp, v)


_sticky_dp = {}
# Typical display resolution, so an exact 2.0 lb still reads "2.00".
MIN_DP = {"lb": 2, "kg": 3, "oz": 1, "otz": 3, "pwt": 2}


def parse_body(raw, content_type):
    """Return (dict_or_None, error_message_or_None)."""
    text = raw.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
    if not text:
        return None, "empty body"
    try:
        data = json.loads(text)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        if isinstance(data, dict):
            return data, None
        return None, "JSON is not an object"
    except ValueError as e:
        err = "invalid JSON: %s" % e
    # Fallback: form-encoded body (Weight=1.2&Unit=kg)
    if "=" in text:
        qs = urllib.parse.parse_qs(text, keep_blank_values=True)
        if qs:
            return {k: v[-1] for k, v in qs.items()}, None
    return None, err


def normalize(data, decimals=None):
    lower = {str(k).strip().lower(): v for k, v in data.items()}

    def pick(*names):
        for n in names:
            if n in lower:
                return lower[n]
        return None

    weight = to_float(pick("weight", "value", "w"))
    tare = to_float(pick("tare", "t"))
    gross = to_bool(pick("gross", "isgross"))
    stable = to_bool(pick("stable", "isstable", "steady"))
    unit = pick("unit", "units", "u")
    platform = to_int(pick("platform", "scale", "channel", "p"))
    unit = str(unit).strip() if unit is not None else ""
    if decimals is None:
        # Keep the widest precision seen for this unit so 65.0 doesn't flicker to "65".
        dp = max(_sticky_dp.get(unit.lower(), MIN_DP.get(unit.lower(), 0)),
                 auto_decimals(weight), auto_decimals(tare))
        _sticky_dp[unit.lower()] = dp
    else:
        dp = decimals
    return {
        "weight": weight,
        "weight_text": fmt(weight, dp),
        "tare": tare,
        "tare_text": fmt(tare, dp),
        "gross": gross,
        "stable": stable,
        "unit": unit,
        "platform": platform,
    }


# ---------------------------------------------------------------------------
# Shared state + SSE broadcasting
# ---------------------------------------------------------------------------

class State:
    def __init__(self, port, decimals, scale_ip="", scale_port=8080):
        self.lock = threading.Lock()
        self.port = port
        self.decimals = decimals
        self.scale_ip = scale_ip
        self.scale_port = scale_port
        self.started = time.time()
        self.count = 0
        self.errors = 0
        self.latest = None
        self.log = collections.deque(maxlen=LOG_SIZE)
        self.times = collections.deque()
        self.clients = set()

    def pps(self, now=None):
        now = now or time.time()
        with self.lock:
            while self.times and now - self.times[0] > 5.0:
                self.times.popleft()
            return len(self.times) / 5.0

    def record(self, entry):
        with self.lock:
            self.count += 1
            entry["seq"] = self.count
            if not entry.get("ok"):
                self.errors += 1
            self.times.append(entry["t"])
            self.log.append(entry)
            if entry.get("ok"):
                self.latest = entry
        self.broadcast("push", entry)

    def snapshot(self):
        with self.lock:
            latest = self.latest
            log = list(self.log)
            count, errors = self.count, self.errors
        return {
            "server": server_info(self.port),
            "scale_default": {"ip": self.scale_ip, "port": self.scale_port},
            "latest": latest,
            "log": log,
            "stats": self.stats(count, errors),
        }

    def stats(self, count=None, errors=None):
        if count is None:
            with self.lock:
                count, errors = self.count, self.errors
        return {
            "count": count,
            "errors": errors,
            "pps": round(self.pps(), 2),
            "now": time.time(),
            "uptime": round(time.time() - self.started),
            "stale_after": STALE_SECONDS,
        }

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def broadcast(self, event, payload):
        msg = "event: %s\ndata: %s\n\n" % (event, json.dumps(payload))
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # no packet is actually sent
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in ips:
        if ip not in out and not ip.startswith("127."):
            out.append(ip)
    return out or ["127.0.0.1"]


_ip_cache = {"t": 0, "ips": []}


def server_info(port):
    if time.time() - _ip_cache["t"] > 30:
        _ip_cache["ips"] = lan_ips()
        _ip_cache["t"] = time.time()
    ips = _ip_cache["ips"]
    return {
        "hostname": socket.gethostname(),
        "ips": ips,
        "port": port,
        "push_urls": ["http://%s:%d/weight" % (ip, port) for ip in ips],
        "version": VERSION,
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ScalePushMonitor/" + VERSION
    state = None  # type: State
    verbose = False

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write("[http] %s - %s\n" % (self.client_address[0], fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            while True:
                line = self.rfile.readline(65537)
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline(65537)
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(length, 1024 * 1024)) if length > 0 else b""

    # -- push receiver -----------------------------------------------------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.lower() == "/api/scale":
            return self._scale_command()
        try:
            raw = self._read_body()
        except Exception as e:  # malformed framing: still answer 200
            raw = b""
            print("[warn] could not read body from %s: %s" % (self.client_address[0], e))
        now = time.time()
        ctype = self.headers.get("Content-Type", "")
        data, err = parse_body(raw, ctype)
        entry = {
            "t": now,
            "time": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "client": self.client_address[0],
            "path": path,
            "content_type": ctype,
            "user_agent": self.headers.get("User-Agent", ""),
            "bytes": len(raw),
            "raw": raw.decode("utf-8", errors="replace")[:4000],
            "ok": data is not None,
            "error": err,
            "data": normalize(data, self.state.decimals) if data is not None else None,
        }
        if path not in ("/", "/weight"):
            entry["note"] = "unexpected path (accepted anyway)"
        self.state.record(entry)
        print_push(entry, self.verbose)
        self._send(200, json.dumps({"ok": True}))

    def do_PUT(self):
        self.do_POST()

    # -- commands to the scale (proxied, the browser can't call it cross-origin)
    def _scale_command(self):
        try:
            req = json.loads(self._read_body().decode("utf-8") or "{}")
            ip = str(req.get("ip", "")).strip()
            port = int(req.get("port") or 0)
            platform = req.get("platform")
            platform = None if platform in (None, "", "0", 0) else int(platform)
            action = str(req.get("action", ""))
            target = req.get("target")
            if not HOST_RE.match(ip):
                raise ValueError("enter a valid scale IP address or hostname")
            if not 1 <= port <= 65535:
                raise ValueError("enter a valid scale port (1-65535)")
            if platform is not None and not 0 <= platform <= 9:
                raise ValueError("platform must be 0-9")
        except (ValueError, TypeError) as e:
            return self._send(400, json.dumps({"ok": False, "error": str(e)}))
        result = run_scale_action(ip, port, action, platform, target)
        result["action"] = action
        print_command(result, self.client_address[0])
        self._send(200, json.dumps(result))

    # -- dashboard ---------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/dashboard"):
            self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(self.state.snapshot()))
        elif path == "/events":
            self._sse()
        elif path == "/weight":
            self._send(200, json.dumps({"ok": True, "hint": "POST JSON here",
                                        "latest": self.state.latest}))
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_HEAD(self):
        self.do_GET()

    def _sse(self):
        q = self.state.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            snap = "event: snapshot\ndata: %s\n\n" % json.dumps(self.state.snapshot())
            self.wfile.write(b"retry: 1500\n\n" + snap.encode("utf-8"))
            self.wfile.flush()
            last_stats = 0.0
            while True:
                try:
                    msg = q.get(timeout=0.5)
                    self.wfile.write(msg.encode("utf-8"))
                except queue.Empty:
                    pass
                if time.time() - last_stats >= 1.0:
                    last_stats = time.time()
                    stats = "event: stats\ndata: %s\n\n" % json.dumps(self.state.stats())
                    self.wfile.write(stats.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.state.unsubscribe(q)


def print_push(e, verbose):
    d = e.get("data")
    if d:
        line = "#%-5d %s  %-15s %s  %s %s  tare=%s  %s  %s  P%s" % (
            e["seq"], e["time"][11:], e["client"], e["path"],
            d["weight_text"] or "-", d["unit"], d["tare_text"] or "-",
            "GROSS" if d["gross"] else "NET",
            "stable" if d["stable"] is not False else "UNSTABLE",
            "-" if d["platform"] is None else d["platform"])
    else:
        line = "#%-5d %s  %-15s %s  [bad body: %s] %r" % (
            e["seq"], e["time"][11:], e["client"], e["path"], e["error"], e["raw"][:120])
    print(line, flush=True)
    if verbose:
        print("       UA=%s  CT=%s  %d bytes\n       %s" % (
            e["user_agent"], e["content_type"], e["bytes"], e["raw"]), flush=True)


# ---------------------------------------------------------------------------
# Scale HTTP API client (Arlyn UpScale REST API 1.0)
# ---------------------------------------------------------------------------

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")

# Only these documented endpoints can be reached through /api/scale.
SCALE_ACTIONS = {
    "zero": ("POST", "button/zero/click"),
    "tare": ("POST", "button/tare/click"),
    "clear_tare": ("POST", "button/tare/longclick"),
    "unit": ("POST", "button/unit/click"),
    "gross": ("POST", "button/gross/click"),
    "get_weight": ("GET", "weight"),
    "get_unit": ("GET", "unit"),
    "get_tare": ("GET", "tare"),
    "get_gross": ("GET", "gross"),
}

UNIT_ALIASES = {
    "lb": ("lb", "pound", "pounds"), "kg": ("kg", "kilogram", "kilograms"),
    "g": ("g", "gram", "grams"), "oz": ("oz", "ounce", "ounces"),
    "otz": ("otz",), "pwt": ("pwt",), "gr": ("gr",), "gal": ("gal", "gallon", "gallons"),
    "perc": ("perc",), "n": ("n",), "jn": ("jn",),
}
MAX_UNIT_CLICKS = 12

# Bypass any system/corporate proxy (urllib reads the Windows proxy settings).
_scale_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def canonical_unit(u):
    u = str(u or "").strip().lower()
    for key, names in UNIT_ALIASES.items():
        if u in names:
            return key
    return u


def scale_request(ip, port, method, path, platform=None, timeout=4.0):
    url = "http://%s:%d/api/1.0/%s" % (ip, port, path)
    headers = {"User-Agent": "ScalePushMonitor/" + VERSION}
    data = None
    if platform is not None:
        url += "?Platform=%d" % platform
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = ("Platform=%d" % platform).encode() if platform is not None else b""
    t0 = time.time()
    out = {"method": method, "url": url}
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with _scale_opener.open(req, timeout=timeout) as r:
            out["status"] = r.status
            body = r.read(65536)
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        body = e.read(65536) if e.fp else b""
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        reason = getattr(e, "reason", e)
        out.update(ok=False, status=None, error="cannot reach scale: %s" % reason,
                   elapsed_ms=round((time.time() - t0) * 1000))
        return out
    out["elapsed_ms"] = round((time.time() - t0) * 1000)
    out["ok"] = 200 <= out["status"] < 300
    out["body"] = body.decode("utf-8", errors="replace").strip()
    try:
        out["json"] = json.loads(out["body"]) if out["body"] else None
    except ValueError:
        out["json"] = None
    if not out["ok"]:
        out["error"] = "scale replied HTTP %s" % out["status"]
    return out


def _json_field(resp, name):
    j = resp.get("json")
    if isinstance(j, dict):
        for k, v in j.items():
            if k.lower() == name.lower():
                return v
    return None


def run_scale_action(ip, port, action, platform=None, target=None):
    """Run one dashboard action; returns a result dict with every HTTP step."""
    if action in SCALE_ACTIONS:
        method, path = SCALE_ACTIONS[action]
        r = scale_request(ip, port, method, path, platform)
        return dict(r, steps=[r])

    steps = []

    def step(method, path):
        r = scale_request(ip, port, method, path, platform)
        steps.append(r)
        return r

    if action == "set_unit":
        want = canonical_unit(target)
        if want not in UNIT_ALIASES:
            return {"ok": False, "error": "unknown unit %r" % target, "steps": steps}
        seen = []
        for _ in range(MAX_UNIT_CLICKS + 1):
            r = step("GET", "unit")
            if not r["ok"]:
                return {"ok": False, "error": r.get("error"), "steps": steps}
            cur = canonical_unit(_json_field(r, "Unit"))
            if cur == want:
                return {"ok": True, "message": "unit is now %s" % cur, "steps": steps}
            if cur in seen:
                break  # went all the way round
            seen.append(cur)
            c = step("POST", "button/unit/click")
            if not c["ok"]:
                return {"ok": False, "error": c.get("error"), "steps": steps}
            time.sleep(0.3)
        return {"ok": False, "steps": steps,
                "error": "unit %s not reachable; scale cycles through: %s" % (want, ", ".join(seen))}

    if action == "set_mode":
        want_gross = str(target).lower() == "gross"
        for attempt in range(2):
            r = step("GET", "gross")
            if not r["ok"]:
                return {"ok": False, "error": r.get("error"), "steps": steps}
            if to_bool(_json_field(r, "Gross")) == want_gross:
                return {"ok": True, "message": "mode is now %s" % ("GROSS" if want_gross else "NET"),
                        "steps": steps}
            if attempt == 0:
                c = step("POST", "button/gross/click")
                if not c["ok"]:
                    return {"ok": False, "error": c.get("error"), "steps": steps}
                time.sleep(0.3)
        return {"ok": False, "error": "scale did not switch mode", "steps": steps}

    return {"ok": False, "error": "unknown action %r" % action, "steps": steps}


def print_command(res, from_ip):
    steps = res.get("steps") or []
    detail = "; ".join("%s %s -> %s" % (s["method"], s["url"], s.get("status") or s.get("error"))
                       for s in steps)
    print("[cmd]  %-10s %s  (from %s)  %s" % (
        res.get("action"), "OK" if res.get("ok") else "FAILED: %s" % res.get("error"),
        from_ip, detail), flush=True)


# ---------------------------------------------------------------------------
# Built-in demo generator (--demo)
# ---------------------------------------------------------------------------

def load_fake_scale():
    sys.path.insert(0, __import__("os").path.dirname(__file__))
    try:
        import simulate
        return simulate
    except ImportError:
        print("[demo] simulate.py not found next to this script; demo disabled")
        return None


def demo_loop(port, rate, scale):
    url = "http://127.0.0.1:%d/weight" % port
    time.sleep(0.5)
    while True:
        body = json.dumps(scale.step(1.0 / rate)).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "V9Scale-WAPIPush/1.0 (demo)"})
        try:
            urllib.request.urlopen(req, timeout=2).read()
        except Exception:
            pass
        time.sleep(1.0 / rate)


# ---------------------------------------------------------------------------
# Dashboard (single page, no external assets so it works offline)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scale Push Monitor</title>
<style>
:root{
  --bg:#0b1020; --panel:#121a2e; --panel2:#18223b; --line:#25304d; --text:#e7ecf7;
  --muted:#8b97b5; --accent:#5b8cff; --good:#22c55e; --warn:#f59e0b; --bad:#ef4444; --net:#38bdf8; --gross:#c084fc;
}
*{box-sizing:border-box}
html,body{margin:0;background:radial-gradient(1200px 600px at 10% -10%,#1b2650 0,transparent 60%),var(--bg);color:var(--text);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;min-height:100vh}
.wrap{max-width:1280px;margin:0 auto;padding:20px 24px 32px}
header{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.logo{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#5b8cff,#8b5cf6);display:grid;place-items:center;font-weight:800}
h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.2px}
.sub{color:var(--muted);font-size:12.5px}
.conn{margin-left:auto;display:flex;align-items:center;gap:8px;padding:7px 12px;border-radius:999px;background:var(--panel);border:1px solid var(--line);font-size:12.5px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted);box-shadow:0 0 0 3px rgba(139,151,181,.15)}
.dot.on{background:var(--good);box-shadow:0 0 0 3px rgba(34,197,94,.18)}
.dot.off{background:var(--bad);box-shadow:0 0 0 3px rgba(239,68,68,.18)}
.dot.warn{background:var(--warn);box-shadow:0 0 0 3px rgba(245,158,11,.18)}
.grid{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:18px}
@media (max-width:960px){.grid{grid-template-columns:1fr}}
.card{background:linear-gradient(180deg,var(--panel),#0f1628);border:1px solid var(--line);border-radius:16px;padding:18px 20px}
.card h2{margin:0 0 12px;font-size:12px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);font-weight:600}
.hero{position:relative;overflow:hidden;padding:22px 26px 20px;display:flex;flex-direction:column}
.badges{display:flex;gap:8px;flex-wrap:wrap}
.badge{padding:5px 11px;border-radius:999px;font-weight:700;font-size:12px;letter-spacing:.8px;border:1px solid transparent}
.b-net{background:rgba(56,189,248,.12);color:var(--net);border-color:rgba(56,189,248,.35)}
.b-gross{background:rgba(192,132,252,.12);color:var(--gross);border-color:rgba(192,132,252,.35)}
.b-stable{background:rgba(34,197,94,.12);color:var(--good);border-color:rgba(34,197,94,.35)}
.b-unstable{background:rgba(245,158,11,.12);color:var(--warn);border-color:rgba(245,158,11,.4)}
.b-stale{background:rgba(239,68,68,.12);color:var(--bad);border-color:rgba(239,68,68,.4)}
.b-plat{background:var(--panel2);color:var(--text);border-color:var(--line)}
.b-hidden{display:none}
.weight{flex:1;display:flex;align-items:center;justify-content:flex-end;gap:16px;margin:18px 0 6px;font-variant-numeric:tabular-nums}
.wval{font-size:clamp(72px,11vw,160px);font-weight:700;letter-spacing:-3px;line-height:1;font-variant-numeric:tabular-nums;transition:color .2s,opacity .2s}
.wunit{align-self:flex-end;margin-bottom:.35em}
.wunit{font-size:clamp(24px,3vw,40px);color:var(--muted);font-weight:600;min-width:2.2em}
.hero.unstable .wval{color:#fde68a}
.hero.stale .wval{opacity:.35}
.hero.stale::after{content:"NO DATA";position:absolute;right:22px;top:20px;color:var(--bad);font-weight:800;letter-spacing:2px;font-size:12px}
.pulse{position:absolute;left:0;top:0;height:3px;width:100%;background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:0;transition:opacity .6s}
.pulse.go{opacity:1;transition:none}
.kv{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:16px}
@media (max-width:640px){.kv{grid-template-columns:repeat(2,1fr)}}
.tile{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.tile .k{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.9px}
.tile .v{font-size:20px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .v small{font-size:12px;color:var(--muted);font-weight:500}
.side{display:flex;flex-direction:column;gap:18px}
.url{display:flex;gap:8px;align-items:center;background:#0a1122;border:1px solid var(--line);border-radius:10px;padding:8px 8px 8px 12px;margin-bottom:8px}
.url code{flex:1;font:13.5px ui-monospace,Consolas,Menlo,monospace;color:#cfe0ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:6px 12px;font-weight:600;font-size:12.5px;cursor:pointer}
button:hover{filter:brightness(1.1)} button.ghost{background:var(--panel2);color:var(--text);border:1px solid var(--line)}
.rows{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px}
.rows .k{color:var(--muted)} .rows .v{text-align:right;font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hint{color:var(--muted);font-size:12px;margin-top:8px}
.logcard{margin-top:18px}
.loghead{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.loghead h2{margin:0}
.loghead .sp{flex:1}
.log{height:300px;overflow:auto;background:#0a1122;border:1px solid var(--line);border-radius:12px;
  font:12.5px/1.55 ui-monospace,"Cascadia Mono",Consolas,Menlo,monospace}
.log .row{display:grid;grid-template-columns:62px 110px 120px 74px 1fr;gap:10px;padding:4px 12px;border-bottom:1px solid #141d33;white-space:nowrap}
.log .row:first-child{background:rgba(91,140,255,.08)}
.log .row.err{color:#fca5a5}
.log .seq{color:var(--muted)} .log .raw{overflow:hidden;text-overflow:ellipsis;color:#b9c6e4}
.log .row .ok{color:var(--good)}
.empty{color:var(--muted);padding:18px;text-align:center;font-family:system-ui,sans-serif}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(20px);background:#1f2a48;border:1px solid var(--line);
  padding:8px 14px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none}
.toast.show{opacity:1;transform:translateX(-50%)}
footer{color:var(--muted);font-size:12px;margin-top:16px;text-align:center}
.ctrl{margin-top:18px}
.ctrl-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.5fr);gap:22px}
@media (max-width:960px){.ctrl-grid{grid-template-columns:1fr}}
.fields{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,.7fr) minmax(0,1.1fr);gap:10px}
label.f{display:flex;flex-direction:column;gap:5px;color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.8px}
input,select{background:#0a1122;border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px 10px;font:14px system-ui,sans-serif;width:100%}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(91,140,255,.2)}
.linkbtn{background:none;border:0;color:var(--accent);padding:0;font-size:12px;font-weight:500;cursor:pointer}
.conn-row{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
.target{font:12.5px ui-monospace,Consolas,Menlo,monospace;color:#b9c6e4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btns{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.cbtn{height:62px;border-radius:12px;font-size:16px;font-weight:750;letter-spacing:1px;background:var(--panel2);border:1px solid var(--line);color:var(--text);
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px}
.cbtn small{font-size:10.5px;font-weight:500;letter-spacing:.3px;color:var(--muted)}
.cbtn:hover{border-color:var(--accent);filter:none;background:#1c2846}
.cbtn.primary{background:linear-gradient(180deg,#3d6df2,#2f57c9);border-color:#4b78f5}
.cbtn.primary small{color:#dbe5ff}
.cbtn:disabled,.seg button:disabled{opacity:.45;cursor:wait}
.row2{display:flex;gap:14px;margin-top:12px;align-items:center;flex-wrap:wrap}
.row2 .lbl{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.8px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.seg button{background:var(--panel2);border-radius:0;color:var(--muted);padding:8px 14px;font-weight:700;letter-spacing:.8px}
.seg button+button{border-left:1px solid var(--line)}
.seg button.on-net{background:rgba(56,189,248,.16);color:var(--net)}
.seg button.on-gross{background:rgba(192,132,252,.16);color:var(--gross)}
.seg button.on-unit{background:rgba(91,140,255,.18);color:#cfe0ff}
.result{margin-top:12px;padding:9px 12px;border-radius:10px;background:#0a1122;border:1px solid var(--line);font-size:13px;min-height:40px;
  display:flex;gap:10px;align-items:center}
.result.ok{border-color:rgba(34,197,94,.4)} .result.bad{border-color:rgba(239,68,68,.45);color:#fecaca}
.result .ic{font-weight:800} .result.ok .ic{color:var(--good)} .result.bad .ic{color:var(--bad)}
.result code{font:12px ui-monospace,Consolas,Menlo,monospace;color:#b9c6e4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;text-align:right}
.cmdlog{margin-top:10px;max-height:118px;overflow:auto;font:12px/1.6 ui-monospace,Consolas,Menlo,monospace;color:#9fb0d6}
.cmdlog .bad{color:#fca5a5} .cmdlog .ok{color:#b9c6e4}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">&#9878;</div>
    <div>
      <h1>Scale Push Monitor</h1>
      <div class="sub">Live receiver for weighing-scale Web API push &middot; <span id="host">-</span></div>
    </div>
    <div class="conn"><span id="connDot" class="dot"></span><span id="connText">Connecting&hellip;</span></div>
  </header>

  <div class="grid">
    <section class="card hero" id="hero">
      <div class="pulse" id="pulse"></div>
      <div class="badges">
        <span class="badge b-net" id="bMode">NET</span>
        <span class="badge b-stable" id="bStable">STABLE</span>
        <span class="badge b-plat" id="bPlat">PLATFORM -</span>
        <span class="badge b-stale b-hidden" id="bStale">STALE</span>
      </div>
      <div class="weight"><span class="wval" id="wval">---</span><span class="wunit" id="wunit"></span></div>
      <div class="kv">
        <div class="tile"><div class="k">Tare</div><div class="v" id="tare">-</div></div>
        <div class="tile"><div class="k">Unit</div><div class="v" id="unit">-</div></div>
        <div class="tile"><div class="k">Last update</div><div class="v" id="ago">-</div></div>
        <div class="tile"><div class="k">Pushes / sec</div><div class="v" id="pps">0</div></div>
      </div>
    </section>

    <aside class="side">
      <section class="card">
        <h2>Program this URL into the scale</h2>
        <div id="urls"><div class="url"><code>&hellip;</code></div></div>
        <div class="hint">Method <b>POST</b>, JSON body. <code>/</code> is accepted too. Allow the port through the firewall.</div>
      </section>
      <section class="card">
        <h2>Status</h2>
        <div class="rows">
          <div class="k">Server IP(s)</div><div class="v" id="ips">-</div>
          <div class="k">Port</div><div class="v" id="port">-</div>
          <div class="k">Sender IP</div><div class="v" id="sender">-</div>
          <div class="k">User-Agent</div><div class="v" id="ua">-</div>
          <div class="k">Last push</div><div class="v" id="lastTime">-</div>
          <div class="k">Total pushes</div><div class="v" id="count">0</div>
          <div class="k">Bad bodies</div><div class="v" id="errors">0</div>
          <div class="k">Raw weight</div><div class="v" id="rawW">-</div>
        </div>
      </section>
    </aside>
  </div>

  <section class="card ctrl">
    <h2>Scale control <span class="sub" style="text-transform:none;letter-spacing:0">&middot; sends commands to the scale's HTTP API</span></h2>
    <div class="ctrl-grid">
      <div>
        <div class="fields">
          <label class="f">Scale IP address
            <input id="scIp" placeholder="e.g. 192.168.1.141" autocomplete="off" spellcheck="false"></label>
          <label class="f">Port
            <input id="scPort" type="number" min="1" max="65535" placeholder="8080"></label>
          <label class="f">Platform
            <select id="scPlat"><option value="">Top (default)</option>
              <option>1</option><option>2</option><option>3</option><option>4</option><option>5</option>
              <option>6</option><option>7</option><option>8</option><option>9</option></select></label>
        </div>
        <div class="conn-row">
          <button id="scTest">Test connection</button>
          <button class="linkbtn" id="scUseSender" title="Fill in the IP that the pushes come from">Use sender IP</button>
          <span class="target" id="scTarget"></span>
        </div>
        <div class="result" id="scResult"><span class="ic">&bull;</span><span>Enter the scale's IP and port, then press a button.</span></div>
      </div>
      <div>
        <div class="btns">
          <button class="cbtn primary" data-act="zero">ZERO<small>zero the scale</small></button>
          <button class="cbtn primary" data-act="tare">TARE<small>tare off weight</small></button>
          <button class="cbtn" data-act="unit">UNIT<small>next unit</small></button>
          <button class="cbtn" data-act="gross">NET / GROSS<small>toggle mode</small></button>
        </div>
        <div class="row2">
          <span class="lbl">Mode</span>
          <span class="seg" id="modeSeg"><button data-mode="net">NET</button><button data-mode="gross">GROSS</button></span>
          <span class="lbl">Unit</span>
          <span class="seg" id="unitSeg"><button data-unit="lb">lb</button><button data-unit="kg">kg</button><button data-unit="g">g</button><button data-unit="oz">oz</button></span>
          <button class="cbtn" style="height:36px;font-size:12.5px;padding:0 12px" data-act="clear_tare" title="Long-press TARE">CLEAR TARE</button>
        </div>
        <div class="cmdlog" id="cmdLog"></div>
      </div>
    </div>
  </section>

  <section class="card logcard">
    <div class="loghead">
      <h2>Recent pushes</h2><span class="sub" id="logInfo"></span><span class="sp"></span>
      <button class="ghost" id="pauseBtn">Pause</button>
      <button class="ghost" id="clearBtn">Clear</button>
    </div>
    <div class="log" id="log"><div class="empty">Waiting for the first push&hellip;</div></div>
  </section>
  <footer>Scale Push Monitor &middot; data stays on this PC</footer>
</div>
<div class="toast" id="toast">Copied</div>

<script>
const $ = id => document.getElementById(id);
let latest = null, lastPushAt = 0, staleAfter = 3, clockOffset = 0, paused = false, logRows = [];
const MAX_LOG = 50;

function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function toast(t){const e=$("toast");e.textContent=t;e.classList.add("show");setTimeout(()=>e.classList.remove("show"),1200)}
function copy(text){
  const done=()=>toast("Copied "+text);
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done,()=>fallback())}else fallback();
  function fallback(){const t=document.createElement("textarea");t.value=text;document.body.appendChild(t);t.select();
    try{document.execCommand("copy");done()}catch(e){prompt("Copy this URL:",text)}t.remove()}
}

function renderServer(s){
  if(!s) return;
  $("host").textContent = s.hostname;
  $("ips").textContent = s.ips.join(", ");
  $("port").textContent = s.port;
  $("urls").innerHTML = s.push_urls.map(u =>
    `<div class="url"><code title="${esc(u)}">${esc(u)}</code><button data-u="${esc(u)}">Copy</button></div>`).join("");
  $("urls").querySelectorAll("button").forEach(b => b.onclick = () => copy(b.dataset.u));
}

function renderLatest(e, flash){
  if(!e || !e.data) return;
  latest = e; lastPushAt = e.t;
  const d = e.data;
  $("wval").textContent = d.weight_text ?? "---";
  $("wunit").textContent = d.unit || "";
  $("unit").textContent = d.unit || "-";
  $("tare").innerHTML = d.tare_text != null ? `${esc(d.tare_text)} <small>${esc(d.unit||"")}</small>` : "-";
  const gross = d.gross === true;
  $("bMode").textContent = gross ? "GROSS" : "NET";
  $("bMode").className = "badge " + (gross ? "b-gross" : "b-net");
  const st = d.stable !== false;
  $("bStable").textContent = d.stable == null ? "STABLE ?" : (st ? "STABLE" : "UNSTABLE");
  $("bStable").className = "badge " + (st ? "b-stable" : "b-unstable");
  $("hero").classList.toggle("unstable", !st);
  $("bPlat").textContent = "PLATFORM " + (d.platform ?? "-");
  $("sender").textContent = e.client;
  $("ua").textContent = e.user_agent || "-"; $("ua").title = e.user_agent || "";
  $("lastTime").textContent = e.time.slice(11);
  $("rawW").textContent = d.weight != null ? String(d.weight) : "-";
  document.querySelectorAll("#modeSeg button").forEach(b => b.className =
    b.dataset.mode === (gross ? "gross" : "net") ? "on-" + b.dataset.mode : "");
  const u = (d.unit || "").toLowerCase();
  document.querySelectorAll("#unitSeg button").forEach(b => b.className = b.dataset.unit === u ? "on-unit" : "");
  if(flash){const p=$("pulse");p.classList.add("go");requestAnimationFrame(()=>requestAnimationFrame(()=>p.classList.remove("go")))}
}

function rowHtml(e){
  const d=e.data, cls=e.ok?"":"err";
  const summary = d ? `${d.weight_text} ${esc(d.unit)}` : "BAD BODY";
  return `<div class="row ${cls}"><span class="seq">#${e.seq}</span><span>${esc(e.time.slice(11))}</span>`+
    `<span>${esc(e.client)}</span><span>${esc(summary)}</span><span class="raw" title="${esc(e.raw)}">${esc(e.error?("["+e.error+"] "):"")}${esc(e.raw.replace(/\s+/g," "))}</span></div>`;
}
function renderLog(){
  if(!logRows.length){$("log").innerHTML='<div class="empty">Waiting for the first push&hellip;</div>';return}
  $("log").innerHTML = logRows.slice().reverse().map(rowHtml).join("");
  $("logInfo").textContent = `last ${logRows.length}` + (paused ? " (paused)" : "");
}
function addLog(e){ if(paused) return; logRows.push(e); if(logRows.length>MAX_LOG) logRows.shift(); renderLog(); }

function renderStats(s){
  if(!s) return;
  clockOffset = s.now*1000 - Date.now();
  staleAfter = s.stale_after || 3;
  $("count").textContent = s.count; $("errors").textContent = s.errors;
  $("pps").textContent = s.pps.toFixed(1);
}

let connected = false;
function tick(){
  const now = (Date.now()+clockOffset)/1000;
  const age = lastPushAt ? now - lastPushAt : null;
  const stale = age == null || age > staleAfter;
  $("ago").textContent = age == null ? "never" : (age < 1 ? "just now" : age < 60 ? age.toFixed(1)+" s ago" : Math.round(age/60)+" min ago");
  $("hero").classList.toggle("stale", stale && !!latest);
  $("bStale").classList.toggle("b-hidden", !(stale && latest));
  const dot=$("connDot"), txt=$("connText");
  if(!connected){dot.className="dot off";txt.textContent="Dashboard disconnected - retrying"}
  else if(age == null){dot.className="dot warn";txt.textContent="Listening - waiting for scale"}
  else if(stale){dot.className="dot warn";txt.textContent="No data for "+Math.round(age)+" s"}
  else {dot.className="dot on";txt.textContent="Receiving from "+(latest?latest.client:"scale")}
}
setInterval(tick, 200);

function connect(){
  const es = new EventSource("/events");
  es.onopen = () => { connected = true; tick(); };
  es.onerror = () => { connected = false; tick(); };
  es.addEventListener("snapshot", m => {
    const s = JSON.parse(m.data);
    renderServer(s.server); renderStats(s.stats);
    if(!scaleCfgLoaded){ scaleCfgLoaded = true; loadScaleCfg(s.scale_default); }
    logRows = s.log.slice(-MAX_LOG); renderLog();
    if(s.latest) renderLatest(s.latest, false);
  });
  es.addEventListener("push", m => {
    const e = JSON.parse(m.data);
    if(e.ok) renderLatest(e, true);
    addLog(e);
  });
  es.addEventListener("stats", m => renderStats(JSON.parse(m.data)));
}
$("pauseBtn").onclick = () => { paused=!paused; $("pauseBtn").textContent = paused?"Resume":"Pause"; renderLog(); };
$("clearBtn").onclick = () => { logRows=[]; renderLog(); $("logInfo").textContent=""; };

// ---- Scale control ------------------------------------------------------
const LS = "spm.scale";
function loadScaleCfg(def){
  let c = {}; try { c = JSON.parse(localStorage.getItem(LS) || "{}"); } catch(e) {}
  if(!c.ip && def && def.ip) c.ip = def.ip;
  if(!c.port && def && def.port) c.port = def.port;
  if(c.ip) $("scIp").value = c.ip;
  $("scPort").value = c.port || 8080;
  $("scPlat").value = c.platform || "";
  showTarget();
}
function scaleCfg(){ return { ip: $("scIp").value.trim(), port: parseInt($("scPort").value, 10) || 0, platform: $("scPlat").value }; }
function saveScaleCfg(){ localStorage.setItem(LS, JSON.stringify(scaleCfg())); showTarget(); }
function showTarget(){
  const c = scaleCfg();
  $("scTarget").textContent = c.ip ? `http://${c.ip}:${c.port || "?"}/api/1.0/` : "";
}
["scIp","scPort","scPlat"].forEach(id => $(id).addEventListener("input", saveScaleCfg));
$("scUseSender").onclick = () => {
  if(latest && latest.client){ $("scIp").value = latest.client; saveScaleCfg(); toast("Using " + latest.client); }
  else toast("No push received yet");
};

const LABELS = {zero:"ZERO", tare:"TARE", clear_tare:"CLEAR TARE", unit:"UNIT", gross:"NET/GROSS",
  get_weight:"TEST", set_unit:"SET UNIT", set_mode:"SET MODE"};
let busy = false;
function setBusy(b){ busy = b; document.querySelectorAll(".ctrl button:not(.linkbtn)").forEach(x => x.disabled = b); }
function showResult(ok, text, detail){
  const r = $("scResult"); r.className = "result " + (ok ? "ok" : "bad");
  r.innerHTML = `<span class="ic">${ok ? "&#10003;" : "&#10007;"}</span><span>${esc(text)}</span><code title="${esc(detail||"")}">${esc(detail||"")}</code>`;
}
function logCmd(ok, text){
  const t = new Date().toLocaleTimeString();
  const div = document.createElement("div"); div.className = ok ? "ok" : "bad";
  div.textContent = `${t}  ${ok ? "OK  " : "FAIL"}  ${text}`;
  const l = $("cmdLog"); l.prepend(div); while(l.children.length > 20) l.lastChild.remove();
}
async function sendCmd(action, target){
  if(busy) return;
  const c = scaleCfg();
  if(!c.ip){ showResult(false, "Enter the scale's IP address first."); $("scIp").focus(); return; }
  const label = LABELS[action] + (target ? " " + String(target).toUpperCase() : "");
  setBusy(true);
  showResult(true, `Sending ${label}...`, "");
  try {
    const resp = await fetch("/api/scale", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({...c, action, target})});
    const r = await resp.json();
    const steps = r.steps || [];
    const last = steps[steps.length - 1] || r;
    const ms = steps.reduce((a, s) => a + (s.elapsed_ms || 0), 0);
    let text;
    if(r.ok){
      text = r.message ? `${label}: ${r.message}` : `${label} sent - HTTP ${last.status} in ${ms} ms`;
      if(action === "get_weight" && r.json){
        const j = {}; Object.keys(r.json).forEach(k => j[k.toLowerCase()] = r.json[k]);
        text = `Connected - scale reads ${j.weight ?? "?"} ${j.unit ?? ""} ${j.gross ? "GROSS" : "NET"}` +
               (j.platform != null ? `, platform ${j.platform}` : "");
      }
    } else text = `${label} failed: ${r.error || "unknown error"}`;
    const detail = steps.map(s => `${s.method} ${s.url} -> ${s.status ?? "no reply"}`).join("  |  ");
    showResult(!!r.ok, text, last.body ? `${last.url}  ->  ${last.body}` : detail);
    logCmd(!!r.ok, text + (steps.length > 1 ? `  (${steps.length} requests)` : ""));
  } catch(e) {
    showResult(false, `${label} failed: dashboard lost contact with the monitor (${e})`);
    logCmd(false, label + " - monitor unreachable");
  } finally { setBusy(false); }
}
document.querySelectorAll(".ctrl [data-act]").forEach(b => b.onclick = () => sendCmd(b.dataset.act));
document.querySelectorAll("#modeSeg button").forEach(b => b.onclick = () => sendCmd("set_mode", b.dataset.mode));
document.querySelectorAll("#unitSeg button").forEach(b => b.onclick = () => sendCmd("set_unit", b.dataset.unit));
$("scTest").onclick = () => sendCmd("get_weight");
let scaleCfgLoaded = false;
connect();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Receive weighing-scale Web API pushes and show a live dashboard.")
    ap.add_argument("port_pos", nargs="?", type=int, help=argparse.SUPPRESS)
    ap.add_argument("-p", "--port", type=int, default=8080, help="TCP port to listen on (default 8080)")
    ap.add_argument("--host", default="0.0.0.0", help="address to bind (default 0.0.0.0 = all interfaces)")
    ap.add_argument("--decimals", type=int, default=None,
                    help="force N decimals for weight/tare (default: auto, e.g. 64.9000015 -> 64.9)")
    ap.add_argument("--scale-ip", default="", help="pre-fill the scale's IP address in the dashboard")
    ap.add_argument("--scale-port", type=int, default=8080, help="pre-fill the scale's HTTP API port (default 8080)")
    ap.add_argument("--demo", action="store_true",
                    help="also generate fake pushes and serve a mock scale API locally (for testing)")
    ap.add_argument("--demo-rate", type=float, default=5.0, help="fake pushes per second with --demo (default 5)")
    ap.add_argument("--demo-api-port", type=int, default=8091, help="mock scale API port with --demo (default 8091)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print headers and full body of every push")
    args = ap.parse_args(argv)
    port = args.port_pos or args.port

    demo_scale = None
    if args.demo:
        sim = load_fake_scale()
        if sim:
            demo_scale = sim.FakeScale()
            try:
                sim.serve_api(demo_scale, args.demo_api_port, host="127.0.0.1")
                args.scale_ip, args.scale_port = "127.0.0.1", args.demo_api_port
            except OSError as e:
                print("[demo] cannot start mock scale API on port %d: %s" % (args.demo_api_port, e))

    state = State(port, args.decimals, args.scale_ip, args.scale_port)
    Handler.state = state
    Handler.verbose = args.verbose
    try:
        httpd = ThreadingHTTPServer((args.host, port), Handler)
    except OSError as e:
        print("ERROR: cannot listen on %s:%d - %s" % (args.host, port, e))
        print("Is another program using that port? Try:  python scale_push_monitor.py --port 8081")
        return 1
    httpd.daemon_threads = True

    info = server_info(port)
    bar = "=" * 64
    print(bar)
    print(" Scale Push Monitor %s" % VERSION)
    print(" Listening on %s:%d" % (args.host, port))
    print(" Dashboard : http://localhost:%d/" % port)
    for u in info["push_urls"]:
        print(" Push URL  : %s   (POST, program this into the scale)" % u)
    print(" Press Ctrl+C to stop.")
    print(bar, flush=True)

    if args.scale_ip:
        print(" Scale API : http://%s:%d/api/1.0/" % (args.scale_ip, args.scale_port), flush=True)
    if demo_scale:
        threading.Thread(target=demo_loop, args=(port, max(0.2, args.demo_rate), demo_scale),
                         daemon=True).start()
        print("[demo] generating fake pushes at %.1f/s; mock scale API on 127.0.0.1:%d"
              % (args.demo_rate, args.demo_api_port))

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
