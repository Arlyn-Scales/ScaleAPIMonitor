# Arlyn UpScale integration guide: connect an Arlyn scale to your software

This guide shows how to connect an [Arlyn UpScale Touchscreen Indicator](https://www.arlynscales.com/scales/arlyn-upscale-touchscreen-indicator-2/) to your own application, MES, ERP, database or script over Ethernet or Wi-Fi. Every example uses plain HTTP and JSON, so it works from any language.

There are two ways to get weight data from the scale:

| | REST HTTP API (pull) | Web API Push |
|---|---|---|
| Who starts the request | Your software calls the scale | The scale calls your software |
| Best for | Reading the weight on demand, and pressing ZERO, TARE, UNIT or NET/GROSS remotely | A continuous stream of readings, with no polling |
| What you need | The scale's IP address and API port | A small HTTP server on your side, and its URL entered in the scale's Web API Push settings |

Many integrations use both: Push for live readings, and the REST API to tare or zero the scale between jobs.

Before you write code, use [ScaleAPIMonitor](https://github.com/Arlyn-Scales/ScaleAPIMonitor) to check that the scale is reachable and sending data. If the dashboard shows live readings and **Test connection** succeeds, your code will be able to reach the scale too. The complete endpoint list is in the [API reference](api-reference.md).

In the examples, replace `192.168.1.141` with your scale's IP address and `8080` with its API port, both from the scale's network settings.

## Read the weight and control the scale (REST API)

The base URL is `http://<scale-ip>:<port>/api/1.0/`.

- `GET weight` returns `{"Gross":false,"Platform":1,"Tare":0.0,"Unit":"lb","Weight":64.9}`.
- `GET unit`, `GET tare` and `GET gross` return just that value.
- `POST button/zero/click`, `button/tare/click`, `button/unit/click` and `button/gross/click` press a button. `POST button/tare/longclick` clears the tare.
- Add `?Platform=n` to choose a platform on a multi-platform scale. Without it, the scale uses the platform shown on screen.

### curl

```bat
curl http://192.168.1.141:8080/api/1.0/weight
curl -X POST http://192.168.1.141:8080/api/1.0/button/tare/click
```

### Python

Uses only the standard library.

```python
import json
import urllib.request

SCALE = "http://192.168.1.141:8080/api/1.0/"

def read(path):
    with urllib.request.urlopen(SCALE + path, timeout=4) as resp:
        return json.load(resp)

def press(button, action="click"):
    req = urllib.request.Request(SCALE + f"button/{button}/{action}", data=b"", method="POST")
    urllib.request.urlopen(req, timeout=4).close()

reading = read("weight")
print(reading["Weight"], reading["Unit"], "GROSS" if reading["Gross"] else "NET")

press("tare")
```

### C# (.NET 6 or newer)

```csharp
using System.Net.Http;
using System.Text.Json;

var http = new HttpClient
{
    BaseAddress = new Uri("http://192.168.1.141:8080/api/1.0/"),
    Timeout = TimeSpan.FromSeconds(4)
};

using var doc = JsonDocument.Parse(await http.GetStringAsync("weight"));
var root = doc.RootElement;
Console.WriteLine($"{root.GetProperty("Weight").GetDouble()} {root.GetProperty("Unit").GetString()}");

await http.PostAsync("button/tare/click", null);
```

### JavaScript (Node.js 18 or newer)

```javascript
const SCALE = "http://192.168.1.141:8080/api/1.0/";

const reading = await (await fetch(SCALE + "weight")).json();
console.log(reading.Weight, reading.Unit);

await fetch(SCALE + "button/tare/click", { method: "POST" });
```

Save it as a `.mjs` file (for example `scale.mjs`) and run it with `node scale.mjs`. Run it on a server or in Node.js, not in a web page. Browsers block a page from calling the scale directly (cross-origin rules), which is why ScaleAPIMonitor's dashboard sends its commands through the monitor.

### PowerShell

```powershell
$scale = "http://192.168.1.141:8080/api/1.0"
Invoke-RestMethod "$scale/weight"
Invoke-RestMethod -Method Post "$scale/button/tare/click"
```

## Receive Web API Push data in your own server

With Web API Push turned on, the UpScale sends one `POST` per reading to the URL you entered in its settings:

```http
POST /weight HTTP/1.1
Content-Type: application/json; charset=utf-8
User-Agent: V9Scale-WAPIPush/1.0

{"Gross": false, "Platform": 1, "Stable": true, "Tare": 0.0, "Unit": "oz", "Weight": 64.9000015258789}
```

Your server should reply quickly with HTTP `200`. The weight is sent as a 32-bit float, so round it for display (`64.9000015258789` is `64.9`). Check `Stable` before you record a weight, so you don't save a reading taken while the load was still settling.

### Python receiver

Uses only the standard library. It listens on port 8080, so the push URL to enter in the scale is `http://<this-PC-IP>:8080/weight`.

```python
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class PushHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        reading = json.loads(body)
        if reading["Stable"]:
            print(f'{reading["Weight"]:.2f} {reading["Unit"]}  platform {reading["Platform"]}')
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

HTTPServer(("0.0.0.0", 8080), PushHandler).serve_forever()
```

This sketch assumes a `Content-Length` header and a valid JSON body. For a version that also handles chunked bodies, form-encoded bodies and malformed data, see the `Handler` class in [`scale_push_monitor.py`](https://github.com/Arlyn-Scales/ScaleAPIMonitor/blob/main/scale_push_monitor.py).

### Node.js receiver

Save it as a `.mjs` file. It listens on port 8080, like the Python receiver.

```javascript
import http from "node:http";

http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => (body += chunk));
  req.on("end", () => {
    const reading = JSON.parse(body);
    if (reading.Stable) console.log(reading.Weight.toFixed(2), reading.Unit);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end('{"ok": true}');
  });
}).listen(8080);
```

On Windows, allow the port through the firewall, or the pushes won't arrive. See [Windows Firewall](user-guide.md#windows-firewall).

## Test your integration without a scale

`simulate.py` acts like an Arlyn UpScale, so you can develop before the hardware arrives:

```bat
python simulate.py --url http://127.0.0.1:8080/weight   :: send pushes to your receiver
python simulate.py --api-port 8090                       :: also serve a mock REST API on port 8090
```

Point your REST code at `http://127.0.0.1:8090/api/1.0/` to try it against the mock API.

## Other ways to connect

Besides the network API, the Arlyn UpScale offers RS-232, RS-422/RS-485, USB (virtual serial), Keyboard Wedge, analog 4-20 mA output, Modbus RTU/ASCII, Modbus TCP, EtherNet/IP and PROFINET, as well as live data logging to Google Sheets and AxChange cloud monitoring. See the [UpScale product page](https://www.arlynscales.com/scales/arlyn-upscale-touchscreen-indicator-2/), or contact [Arlyn Scales](https://www.arlynscales.com/) for help choosing.
