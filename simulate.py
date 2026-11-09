#!/usr/bin/env python3
"""
Fake weighing scale for testing Scale Push Monitor (stdlib only).

It pushes weight JSON to the monitor and can also serve a mock of the
Arlyn UpScale HTTP API (ZERO / TARE / UNIT / GROSS buttons) so the
dashboard's scale controls can be tested without hardware.

    python simulate.py                                   # push to http://127.0.0.1:8080/weight at 5/s
    python simulate.py --api-port 8090                   # ...and serve the mock scale API on port 8090
    python simulate.py --url http://192.168.1.50:8080/weight --rate 10
    python simulate.py --unit kg --platform 2 --bad 0.05 # 5% malformed bodies
    python simulate.py --once                            # send the sample push once and exit
"""

import argparse
import json
import random
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SAMPLE = {"Gross": False, "Platform": 1, "Stable": True, "Tare": 0.0, "Unit": "oz", "Weight": 64.9000015258789}

# unit -> (ounces per unit, display resolution)
UNITS = {"oz": (1.0, 0.1), "lb": (16.0, 0.01), "kg": (35.27396195, 0.005), "g": (0.03527396195, 1.0)}
UNIT_ORDER = ["oz", "lb", "kg", "g"]


def f32(x):
    """Widen through float32, like the real scale does (64.9 -> 64.9000015258789)."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


class FakeScale:
    """Physical load cycles empty -> loading -> loaded -> removing, in ounces.

    ZERO, TARE, UNIT and GROSS/NET behave like the buttons on the display.
    """

    def __init__(self, unit="oz", platform=1):
        self.lock = threading.Lock()
        self.unit = unit if unit in UNITS else "oz"
        self.platform = platform
        self.load = 0.0          # physical load on the platform (oz)
        self.target = 0.0
        self.zero = 0.0          # zero offset (oz)
        self.tare = 0.0          # tare (oz)
        self.gross = False
        self.stable = True
        self.phase = "empty"
        self.phase_t = 0.0

    def _next_phase(self):
        order = {"empty": "loading", "loading": "loaded", "loaded": "removing", "removing": "empty"}
        self.phase = order[self.phase]
        self.phase_t = 0.0
        if self.phase == "loading":
            self.target = random.choice([12.5, 32.0, 64.9, 101.3, 7.8])
        elif self.phase == "removing":
            self.target = 0.0

    def _to_unit(self, oz):
        per, res = UNITS[self.unit]
        return round(round(oz / per / res) * res, 4)

    def step(self, dt):
        with self.lock:
            self.phase_t += dt
            limits = {"empty": 4.0, "loading": 2.0, "loaded": 6.0, "removing": 1.5}
            if self.phase_t > limits[self.phase]:
                self._next_phase()
            settling = self.phase in ("loading", "removing")
            if settling:
                self.load += (self.target - self.load) * min(1.0, dt * 3.0)
                self.load += random.uniform(-1, 1) * 0.8
            else:
                self.load = self.target
            self.stable = not settling
            return self.reading()

    def reading(self):
        gross_w = max(0.0, self.load) - self.zero
        shown = gross_w if self.gross else gross_w - self.tare
        return {
            "Gross": self.gross,
            "Platform": self.platform,
            "Stable": self.stable,
            "Tare": f32(self._to_unit(self.tare)),
            "Unit": self.unit,
            "Weight": f32(self._to_unit(shown)),
        }

    def press(self, button, long=False):
        with self.lock:
            if button == "zero":
                self.zero = max(0.0, self.load)
                self.tare = 0.0
            elif button == "tare":
                if long:
                    self.tare = 0.0
                else:
                    self.tare = max(0.0, self.load) - self.zero
                    self.gross = False
            elif button == "unit":
                self.unit = UNIT_ORDER[(UNIT_ORDER.index(self.unit) + 1) % len(UNIT_ORDER)]
            elif button == "gross":
                self.gross = not self.gross
            else:
                return False
            return True


def make_api_handler(scale, verbose=True):
    class ApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FakeUpScale/1.0"

        def log_message(self, fmt, *args):
            pass

        def _reply(self, code, obj=None):
            body = json.dumps(obj).encode("utf-8") if obj is not None else b""
            self.send_response(code)
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _parts(self):
            u = urllib.parse.urlparse(self.path)
            parts = [p for p in u.path.lower().split("/") if p]
            if parts[:2] != ["api", "1.0"]:
                return None
            return parts[2:]

        def do_GET(self):
            parts = self._parts()
            with scale.lock:
                r = scale.reading()
            res = {
                ("weight",): r,
                ("unit",): {"Platform": r["Platform"], "Unit": r["Unit"]},
                ("tare",): {"Platform": r["Platform"], "Unit": r["Unit"], "Tare": r["Tare"]},
                ("gross",): {"Platform": r["Platform"], "Gross": r["Gross"]},
            }.get(tuple(parts or ()))
            if res is None:
                return self._reply(404, {"error": "not found"})
            self._reply(200, res)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            parts = self._parts() or []
            if len(parts) == 3 and parts[0] == "button" and parts[2] in ("click", "longclick"):
                if scale.press(parts[1], long=parts[2] == "longclick"):
                    if verbose:
                        print("[api] %s %s from %s" % (parts[1].upper(), parts[2], self.client_address[0]), flush=True)
                    return self._reply(200)
            self._reply(404, {"error": "not found"})

    return ApiHandler


def serve_api(scale, port, host="0.0.0.0", verbose=True):
    httpd = ThreadingHTTPServer((host, port), make_api_handler(scale, verbose))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def send(url, payload_bytes, timeout=3):
    req = urllib.request.Request(url, data=payload_bytes, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "V9Scale-WAPIPush/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser(description="Send fake scale pushes (and optionally serve a mock scale API).")
    ap.add_argument("--url", default="http://127.0.0.1:8080/weight", help="push target URL")
    ap.add_argument("--rate", type=float, default=5.0, help="pushes per second (default 5)")
    ap.add_argument("--count", type=int, default=0, help="stop after N pushes (0 = forever)")
    ap.add_argument("--unit", default="oz", help="starting unit: oz, lb, kg or g (default oz)")
    ap.add_argument("--platform", type=int, default=1)
    ap.add_argument("--bad", type=float, default=0.0, help="fraction of malformed bodies to send (0-1)")
    ap.add_argument("--api-port", type=int, default=0,
                    help="also serve a mock UpScale HTTP API on this port (e.g. 8090)")
    ap.add_argument("-q", "--quiet", action="store_true", help="don't print every push")
    ap.add_argument("--once", action="store_true", help="send the captured sample push once and exit")
    args = ap.parse_args()

    if args.once:
        body = json.dumps(SAMPLE, indent=2).encode("utf-8")
        print("POST %s -> HTTP %s" % (args.url, send(args.url, body)))
        return 0

    scale = FakeScale(args.unit, args.platform)
    if args.api_port:
        serve_api(scale, args.api_port)
        print("Mock scale API on http://0.0.0.0:%d/api/1.0/  (enter 127.0.0.1 / %d in the dashboard)"
              % (args.api_port, args.api_port))
    dt = 1.0 / max(0.1, args.rate)
    n = 0
    print("Pushing to %s at %.1f/s - Ctrl+C to stop" % (args.url, args.rate))
    try:
        while not args.count or n < args.count:
            payload = scale.step(dt)
            if args.bad and random.random() < args.bad:
                body = b'{"Weight": 12.3, "Unit": "oz", BROKEN'
            else:
                body = json.dumps(payload).encode("utf-8")
            try:
                status = send(args.url, body)
            except Exception as e:
                status = "ERR %s" % e
            n += 1
            if not args.quiet:
                print("#%-5d HTTP %s  %s" % (n, status, body.decode()), flush=True)
            time.sleep(dt)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
