# ScaleAPIMonitor

A small, dependency-free tool for testing a weighing-scale display's **Web API**. It does two things:

- **Receives "Push" messages.** The scale POSTs its weight to this program, and a live web dashboard shows it.
- **Sends commands.** You can press ZERO, TARE, UNIT and NET/GROSS on the scale remotely, through the Arlyn UpScale HTTP API.

![Dashboard with live data and the Scale control panel](docs/images/dashboard.png)

It is a single Python 3 script that uses only the standard library: no `pip install` and no internet access required. It runs on Windows, macOS and Linux.

## Quick start

1. Install **Python 3.8+** from [python.org](https://www.python.org/downloads/). On Windows, tick **"Add python.exe to PATH"**.
2. Download this repository (**Code > Download ZIP**, or `git clone`) and open a terminal in the folder.
3. Start the monitor:

   ```bat
   python scale_push_monitor.py
   ```

   On Windows, you can double-click `run.bat` instead.
4. Open <http://localhost:8080/>.
5. Copy the **push URL** shown on the dashboard (for example `http://192.168.1.50:8080/weight`) into the scale's Web API Push settings.
6. To control the scale, enter its IP address and API port in the **Scale control** panel, then press **Test connection**.

No scale handy? Run `python scale_push_monitor.py --demo`. It generates fake pushes and runs a fake scale that responds to the buttons.

On Windows, allow Python through the firewall when Windows asks. Otherwise the scale's pushes can't reach your PC. See [Windows Firewall](docs/user-guide.md#windows-firewall).

## Features

| Area | What you get |
|---|---|
| Live reading | Large weight readout with float32 noise removed (`64.9000015258789` shows as `64.9`); unit, tare and platform; STABLE/UNSTABLE; NET/GROSS badge |
| Connection | This PC's LAN IP(s) and port, the push URL with a copy button, last update, pushes per second, sender IP, User-Agent, and a STALE warning after 3 s without data |
| Push log | The last 50 raw push bodies, with malformed ones highlighted; pause and clear |
| Scale control | ZERO, TARE, CLEAR TARE, UNIT, NET/GROSS, plus direct "set unit" (lb/kg/g/oz) and "set mode" (NET/GROSS), each with its result and a command log |
| Robust input | Accepts `POST /weight` or `POST /`; key names in any case; chunked or form-encoded bodies. Bad JSON is logged but never crashes the server, and the scale always gets HTTP 200 |
| Testing | `simulate.py` (fake pushes, plus an optional mock scale API) and the `--demo` flag |

## Documentation

- [User guide](docs/user-guide.md): installation, setting up the scale, a tour of the dashboard, scale control, the simulator, command-line options, firewall and troubleshooting
- [API reference](docs/api-reference.md): what the scale sends, this program's endpoints, and the scale's HTTP API calls it uses
- [Changelog](CHANGELOG.md)

## Files

| File | Purpose |
|---|---|
| `scale_push_monitor.py` | The server and dashboard (the HTML is built in, so it works offline) |
| `simulate.py` | A fake scale: sends pushes, and with `--api-port` also serves a mock UpScale HTTP API |
| `run.bat` | Windows launcher: double-click it, or run `run.bat 9000` for another port |
| `docs/` | The guides and screenshots |

## Security note

The dashboard and its scale controls are open to anyone who can reach this PC on the configured port, and there is no login. Run it only on a trusted local network, and don't expose the port to the internet.
