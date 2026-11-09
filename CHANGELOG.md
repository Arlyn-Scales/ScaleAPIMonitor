# Changelog

## 1.1 - 2026-11-09

- Added a **Scale control** panel: enter the scale's IP, port and platform; buttons for ZERO, TARE, CLEAR TARE, UNIT and NET/GROSS; direct "set unit" and "set mode" buttons; a connection test; a command log.
- Added the `POST /api/scale` endpoint, which forwards the dashboard's commands to the scale's HTTP API. Only the documented endpoints are allowed.
- Added the `--scale-ip` and `--scale-port` options to pre-fill the panel.
- `--demo` now also runs a mock scale API, and `simulate.py --api-port` serves one too.
- Each unit now keeps a minimum number of decimals, so 2.0 lb shows as `2.00`.

## 1.0 - 2026-11-09

- First release: receives pushes at `POST /weight` and `/`, with lenient parsing.
- Live dashboard (Server-Sent Events) showing weight, unit, tare, stability, NET/GROSS, platform, the push URL, connection stats, a stale warning and a raw push log.
- Added the `simulate.py` fake pusher and the `run.bat` Windows launcher.
