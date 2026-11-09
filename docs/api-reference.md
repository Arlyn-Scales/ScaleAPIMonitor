# Arlyn UpScale Web API Push and REST API reference

This page documents the network API of the [Arlyn UpScale Touchscreen Indicator](https://www.arlynscales.com/scales/arlyn-upscale-touchscreen-indicator-2/) as used by ScaleAPIMonitor. It covers three things: the JSON the scale pushes with Web API Push, the HTTP endpoints ScaleAPIMonitor serves, and the UpScale REST HTTP API calls it makes. For ready-to-use code, see the [integration guide](integration-guide.md).

## 1. Web API Push from the scale (incoming)

The UpScale's Web API Push feature sends one request per reading:

```http
POST /weight HTTP/1.1
Content-Type: application/json; charset=utf-8
User-Agent: V9Scale-WAPIPush/1.0

{"Gross": false, "Platform": 1, "Stable": true, "Tare": 0.0, "Unit": "oz", "Weight": 64.9000015258789}
```

| Field | Type | Meaning |
|---|---|---|
| `Weight` | number | Weight in `Unit`. It is NET or GROSS depending on `Gross` |
| `Tare` | number | Tare in `Unit` |
| `Unit` | string | `lb`, `kg`, `g`, `oz`, ... |
| `Gross` | bool | `true` = gross weight, `false` = net weight |
| `Stable` | bool | Stable indicator |
| `Platform` | int | Platform number |

The monitor always replies `200 OK` with `{"ok": true}`, even when it can't parse the body. It is deliberately lenient:

- It accepts `POST /weight`, `POST /`, and POST or PUT to any other path. Other paths are flagged in the log.
- Key names are matched in any case (`weight`, `WEIGHT`). It also accepts `"1.5"`-style numeric strings and `"true"`, `"1"` or `"yes"` booleans.
- It accepts chunked transfer encoding, a UTF-8 byte-order mark (BOM), a JSON array (the first object is used), and form-encoded bodies (`Weight=1.2&Unit=kg`).

## 2. Monitor endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The dashboard (a single HTML page with no external assets) |
| `POST` | `/weight` or `/` | Receives a push (see above) |
| `GET` | `/weight` | A hint, plus the latest parsed push, as JSON |
| `GET` | `/api/state` | A snapshot: server info, default scale address, latest push, recent log, stats |
| `GET` | `/events` | A Server-Sent Events stream used by the dashboard |
| `POST` | `/api/scale` | Sends a command to the scale (see below) |

### `GET /events`

The `/events` stream sends these event types:

| Event | When it's sent | Contents |
|---|---|---|
| `snapshot` | Once, on connect | The same object as `/api/state` |
| `push` | On every push received | A log entry |
| `stats` | Every second | `count`, `errors`, `pps`, `now`, `uptime`, `stale_after` |

A log entry looks like this:

```json
{
  "seq": 52, "t": 1760554277.12, "time": "2026-10-15 14:51:17.120",
  "client": "192.168.1.141", "path": "/weight",
  "content_type": "application/json; charset=utf-8", "user_agent": "V9Scale-WAPIPush/1.0",
  "bytes": 91, "raw": "{...}", "ok": true, "error": null,
  "data": {"weight": 64.9000015258789, "weight_text": "64.9", "tare": 0.0, "tare_text": "0.0",
           "gross": false, "stable": true, "unit": "oz", "platform": 1}
}
```

### `POST /api/scale`

Request body (JSON):

```json
{"ip": "192.168.1.141", "port": 8080, "platform": "", "action": "tare", "target": null}
```

- `platform`: empty for the top (displayed) platform, or `1`-`9`.
- `action`: one of the actions below.
- `target`: only used by `set_unit` (`lb`, `kg`, `g`, `oz`, ...) and `set_mode` (`net` or `gross`).

| `action` | Scale request(s) |
|---|---|
| `zero` | `POST button/zero/click` |
| `tare` | `POST button/tare/click` |
| `clear_tare` | `POST button/tare/longclick` |
| `unit` | `POST button/unit/click` |
| `gross` | `POST button/gross/click` |
| `get_weight`, `get_unit`, `get_tare`, `get_gross` | `GET weight`, `GET unit`, `GET tare`, `GET gross` |
| `set_unit` | `GET unit`, then `POST button/unit/click` until it matches (at most 12 clicks) |
| `set_mode` | `GET gross`, then `POST button/gross/click` if needed, then `GET gross` again |

Response:

```json
{
  "ok": true, "action": "set_unit", "message": "unit is now lb",
  "steps": [
    {"method": "GET",  "url": "http://192.168.1.141:8080/api/1.0/unit", "status": 200, "elapsed_ms": 12, "ok": true,
     "body": "{\"Platform\":1,\"Unit\":\"oz\"}", "json": {"Platform": 1, "Unit": "oz"}},
    {"method": "POST", "url": "http://192.168.1.141:8080/api/1.0/button/unit/click", "status": 200, "...": "..."},
    {"method": "GET",  "url": "http://192.168.1.141:8080/api/1.0/unit", "status": 200, "...": "..."}
  ]
}
```

On failure, `ok` is `false` and `error` explains why, for example `"cannot reach scale: timed out"` or `"scale replied HTTP 404"`. Invalid input (a bad IP or port) returns HTTP 400. Requests to the scale time out after 4 seconds and bypass any system proxy.

## 3. Arlyn UpScale REST HTTP API (outgoing)

The Arlyn UpScale has a built-in REST HTTP API for reading the scale and pressing its buttons over the network. These calls follow the *Arlyn UpScale REST HTTP API Reference*, version 1.0. The base URL is `http://<scale-ip>:<port>/api/1.0/`. The path is case-insensitive, but parameter names are case-sensitive (`Platform`, not `platform`).

| Purpose | Method | Path | Response |
|---|---|---|---|
| Read weight | GET | `weight[?Platform=n]` | `{"Gross":false,"Platform":1,"Tare":102.4,"Unit":"oz","Weight":22.78}` |
| Read unit | GET | `unit[?Platform=n]` | `{"Platform":1,"Unit":"oz"}` |
| Read tare | GET | `tare[?Platform=n]` | `{"Platform":1,"Unit":"oz","Tare":100.123}` |
| Read gross/net | GET | `gross[?Platform=n]` | `{"Platform":1,"Gross":true}` |
| Press UNIT | POST | `button/unit/click` | none |
| Press TARE | POST | `button/tare/click` | none |
| Long-press TARE (clear tare) | POST | `button/tare/longclick` | none |
| Press GROSS/NET | POST | `button/gross/click` | none |
| Press ZERO | POST | `button/zero/click` | none |

`Platform` 0 (the default) means the platform currently on screen. The monitor omits the parameter when "Top (default)" is selected.

The reference document is inconsistent about where `Platform` goes on POST requests: its examples put it in the query string, but its text says parameters go in the request body. When a platform is chosen, the monitor sends it both ways (`?Platform=n` and a form body `Platform=n`).

Unit names the scale may report (case-insensitive): `lb`/`pound(s)`, `kg`/`kilogram(s)`, `g`/`gram(s)`, `oz`/`ounce(s)`, `otz`, `pwt`, `gr`, `gal`/`gallon(s)`, `perc`, `N`, `jn`.
