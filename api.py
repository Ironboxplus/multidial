"""HTTP status/control API for the multi-dial daemon.

`route()` is a pure function over the daemon, so request handling is testable
without a socket. The LuCI dashboard normally reaches these endpoints through an
rpcd ucode proxy (same-origin, LuCI auth); the tiny built-in HTML page is just
for direct on-router debugging.
"""
from __future__ import annotations

import json
import http.server
import socketserver
import urllib.parse
from typing import Tuple


def _json(obj, status=200) -> Tuple[int, str, str]:
    return status, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False)


def _body_json(body: str):
    """Parse a JSON request body -> (dict, error). Empty body => ({}, None)."""
    if not body or not body.strip():
        return {}, None
    try:
        obj = json.loads(body)
    except ValueError:
        return None, "invalid JSON body"
    if not isinstance(obj, dict):
        return None, "body must be a JSON object"
    return obj, None


def _limit(query: str, default: int) -> int:
    try:
        return int(urllib.parse.parse_qs(query).get("limit", [default])[0])
    except (ValueError, TypeError):
        return default


def route(daemon, method: str, path: str, body: str = "") -> Tuple[int, str, str]:
    """Map (method, path, body) to (status, content_type, response)."""
    raw = path
    path = raw.split("?", 1)[0]
    query = raw.split("?", 1)[1] if "?" in raw else ""
    parts = [p for p in path.split("/") if p]

    # --- whole-daemon status + control ---
    if path == "/api/status":
        if method != "GET":
            return _json({"error": "method not allowed"}, 405)
        return _json(daemon.snapshot())

    if path == "/api/reload":
        if method != "POST":
            return _json({"error": "method not allowed"}, 405)
        daemon.force_reload()
        return _json(daemon.snapshot())

    if path == "/api/reauth":
        if method != "POST":
            return _json({"error": "method not allowed"}, 405)
        return _json(daemon.force_reauth())

    if path == "/api/power":
        if method != "POST":
            return _json({"error": "method not allowed"}, 405)
        data, err = _body_json(body)
        if err:
            return _json({"error": err}, 400)
        if "enabled" not in data:
            return _json({"error": "missing 'enabled'"}, 400)
        return _json(daemon.set_power(data.get("enabled")))

    if path == "/api/sqm":
        if method != "GET":
            return _json({"error": "method not allowed"}, 405)
        return _json(daemon.sqm_status())

    if path == "/api/traffic":
        if method != "GET":
            return _json({"error": "method not allowed"}, 405)
        return _json(daemon.traffic())

    if path == "/api/sticky":
        if method == "GET":
            return _json(daemon.sticky_status())
        if method != "POST":
            return _json({"error": "method not allowed"}, 405)
        data, err = _body_json(body)
        if err:
            return _json({"error": err}, 400)
        try:
            return _json(daemon.set_sticky(data))
        except ValueError as e:
            return _json({"error": str(e)}, 400)

    if path == "/api/log":
        if method != "GET":
            return _json({"error": "method not allowed"}, 405)
        return _json({"lines": daemon.log_lines(_limit(query, 200))})

    if path == "/api/history":
        if method != "GET":
            return _json({"error": "method not allowed"}, 405)
        return _json({"events": daemon.history_events(_limit(query, 50))})

    # --- account CRUD ---
    if path == "/api/account":
        if method != "POST":
            return _json({"error": "method not allowed"}, 405)
        data, err = _body_json(body)
        if err:
            return _json({"error": err}, 400)
        try:
            snap = daemon.add_or_update_account(
                name=data.get("name", ""),
                userid=data.get("userid", ""),
                password=data.get("password", ""),
                dials=data.get("dials", 0),
                enabled=data.get("enabled", True),
            )
        except ValueError as e:
            return _json({"error": str(e)}, 400)
        return _json(snap)

    if len(parts) == 3 and parts[0] == "api" and parts[1] == "account":
        if method != "DELETE":
            return _json({"error": "method not allowed"}, 405)
        try:
            return _json(daemon.delete_account(parts[2]))
        except ValueError as e:
            return _json({"error": str(e)}, 400)

    # --- per-dial decommission: DELETE /api/dial/<device> ---
    if len(parts) == 3 and parts[0] == "api" and parts[1] == "dial":
        if method != "DELETE":
            return _json({"error": "method not allowed"}, 405)
        try:
            return _json(daemon.decommission_dial(parts[2]))
        except ValueError as e:
            return _json({"error": str(e)}, 400)

    # --- per-dial actions: /api/dial/<device>/<stop|start|mark|rotate|reauth> ---
    if len(parts) == 4 and parts[0] == "api" and parts[1] == "dial":
        device, action = parts[2], parts[3]
        if method != "POST":
            return _json({"error": "method not allowed"}, 405)
        try:
            if action == "stop":
                return _json(daemon.stop_dial(device))
            if action == "start":
                return _json(daemon.start_dial(device))
            if action == "rotate":
                return _json(daemon.rotate_mac(device))
            if action == "reauth":
                return _json(daemon.reauth_dial(device))
            if action == "mark":
                data, err = _body_json(body)
                if err:
                    return _json({"error": err}, 400)
                return _json(daemon.mark_dial(device, data.get("mark", "")))
        except ValueError as e:
            return _json({"error": str(e)}, 400)
        return _json({"error": "unknown action"}, 404)

    if path in ("/", "/index.html"):
        if method != "GET":
            return _json({"error": "method not allowed"}, 405)
        return 200, "text/html; charset=utf-8", _DEBUG_PAGE

    return _json({"error": "not found"}, 404)


_DEBUG_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>multidial</title><style>body{font:14px monospace;margin:2em}
td,th{padding:.3em .8em;border-bottom:1px solid #ccc;text-align:left}
.on{color:#080}.off{color:#c00}</style></head><body>
<h2>多拨状态 <button onclick=r()>reauth</button></h2>
<div id=agg></div><table id=t></table>
<script>
async function load(){let d=await (await fetch('/api/status')).json();
document.getElementById('agg').textContent=`在线 ${d.aggregate.online}/${d.aggregate.total} · reauths ${d.reauth_count||0}`;
let rows=['<tr><th>dial<th>account<th>online<th>detail'];
for(let x of d.dials)rows.push(`<tr><td>${x.device}<td>${x.account_userid}`+
`<td class=${x.online?'on':'off'}>${x.online?'✓':'✗'}<td>${x.detail||''}`);
document.getElementById('t').innerHTML=rows.join('');}
async function r(){await fetch('/api/reauth',{method:'POST'});load();}
load();setInterval(load,5000);
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    daemon = None  # set by serve()

    def log_message(self, *a):
        pass

    def _dispatch(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        req_body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        try:
            status, ctype, body = route(self.daemon, method, self.path, req_body)
        except Exception as e:  # never 500 the whole server on one bad request
            status, ctype, body = 500, "application/json", json.dumps({"error": str(e)})
        data = body.encode("utf-8")
        self._safe_reply(status, ctype, data)

    def _safe_reply(self, status, ctype, data: bytes) -> None:
        """Write the response, tolerating a client that already hung up.

        Mutating calls (account edit, dial delete) do real work before replying;
        a browser that gave up waiting drops the socket, so the write raises
        BrokenPipeError. The work already committed server-side — swallow the
        write error instead of dumping a traceback and 500-ing a done operation.
        """
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:  # BrokenPipeError is a ConnectionError subclass
            pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # Bypass http.server's getfqdn() reverse-DNS, which needs the optional
        # 'idna' codec that minimal OpenWRT python3-light doesn't ship.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def serve(daemon, host="127.0.0.1", port=23334):
    """Start the blocking HTTP server bound to the daemon."""
    handler = type("BoundHandler", (_Handler,), {"daemon": daemon})
    httpd = _ThreadingServer((host, port), handler)
    httpd.serve_forever()
    return httpd
