#!/usr/bin/env python3
"""webmcp-bridge: browser-buddy -> WebMCP bridge server (stdlib only).

WebMCP (W3C Community Group draft, Chrome origin trial M149+) lets a web page
declare typed, callable tools via ``document.modelContext.registerTool()`` so
an in-browser AI agent can call them directly instead of scraping the DOM.

This server does the reverse half of that loop: it maps Browser Buddy's MCP
tools -- which read pages through the user's *real, logged-in* Chrome profile
(via the Browser Buddy native host on a Unix socket) -- to WebMCP-style
endpoints:

  GET  /webmcp/tools        -> JSON array of WebMCP tool manifests
                               (name, title, description, inputSchema,
                               annotations)
  POST /webmcp/tools/call   -> {"name": ..., "arguments": {...}} forwards to
                               the Browser Buddy native host and returns the
                               result in MCP content-block shape
  GET  /healthz             -> {"ok": true, "version": ...}
  GET  /                   -> demo page
  GET  /webmcp.js           -> page-side shim (registers the tools with
                               document.modelContext, falling back to
                               navigator.modelContext on older preview builds)

Security: binds 127.0.0.1 only. ``open_and_read_url`` enforces the same
domain allowlist semantics as Browser Buddy: ``allowed_domains`` in
~/.config/webmcp-bridge/config.json; an empty list allows everything (with a
warning baked into the result) -- set it to restrict which sites a page-side
agent may open in your logged-in browser.
"""

import argparse
import json
import os
import socket
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__

DEFAULT_PORT = 18765
SERVER_NAME = "webmcp-bridge"
BROWSER_BUDDY_SERVER = "browser-buddy"
HOST_TIMEOUT_SECS = 75

# ---------------------------------------------------------------------------
# WebMCP tool manifests
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "read_active_tab",
        "title": "Read active tab",
        "description": (
            "Read the user's currently active Chrome tab through their real, "
            "logged-in browser profile. Returns the page URL, title, and "
            "cleaned readable text. Use when fetch is blocked by a 403, "
            "login wall, or bot detection."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "untrustedContentHint": True,
            "consequentialHint": False,
            "debugging": False,
        },
    },
    {
        "name": "open_and_read_url",
        "title": "Open URL and read",
        "description": (
            "Open a URL in a background Chrome tab using the user's real "
            "browser profile (cookies and login sessions included), extract "
            "its readable text, then close the tab. For pages behind logins, "
            "paywalls-with-session, or bot checks that defeat server-side "
            "fetchers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "http(s) URL to open and read",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "untrustedContentHint": True,
            "consequentialHint": True,
            "debugging": False,
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def config_path():
    return os.path.join(
        os.path.expanduser("~"), ".config", "webmcp-bridge", "config.json"
    )


_config_cache = None


def load_config():
    """Read ~/.config/webmcp-bridge/config.json. Never raises."""
    global _config_cache
    if _config_cache is None:
        cfg = {}
        try:
            with open(config_path(), "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                cfg = loaded
        except (OSError, ValueError):
            pass
        _config_cache = cfg
    return _config_cache


def reset_config_cache():
    global _config_cache
    _config_cache = None


def browser_buddy_socket_path():
    """Path of the Browser Buddy native host socket (matches its MCP server)."""
    cfg = load_config()
    override = cfg.get("browser_buddy_socket")
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "browser-buddy", "browser-buddy.sock")


def url_allowed(url):
    """Domain allowlist. Empty list = everything allowed (with warning)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    allowed = [d.lower() for d in (load_config().get("allowed_domains") or [])]
    if not allowed:
        return True
    return any(host == d or host.endswith("." + d) for d in allowed)


def empty_allowlist_warning():
    return (
        "WARNING: allowed_domains is empty -- all domains are allowed. "
        "Set allowed_domains in %s to restrict which sites this bridge "
        "can open in your browser.\n\n" % config_path()
    )


# ---------------------------------------------------------------------------
# Browser Buddy native-host forwarding
# ---------------------------------------------------------------------------


def call_host(cmd, args=None, timeout=HOST_TIMEOUT_SECS, socket_path=None):
    """Send one request to the Browser Buddy native host over its socket."""
    payload = {"id": 1, "cmd": cmd}
    if args:
        payload.update(args)
    path = socket_path or browser_buddy_socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(path)
    except OSError:
        s.close()
        return {
            "id": 1,
            "ok": False,
            "error": (
                "browser-buddy host is not reachable at %s. "
                "Is Chrome running with the Browser Buddy extension loaded?"
                % path
            ),
        }
    try:
        with s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            f = s.makefile("r", encoding="utf-8")
            line = f.readline()
            if not line:
                return {"id": 1, "ok": False, "error": "host closed the connection"}
            return json.loads(line)
    except (OSError, json.JSONDecodeError) as exc:
        return {"id": 1, "ok": False, "error": "host communication failed: %s" % exc}


def format_page_result(resp):
    header = "URL: %s\nTitle: %s%s\n\n" % (
        resp.get("url", ""),
        resp.get("title", ""),
        " (truncated)" if resp.get("truncated") else "",
    )
    return header + resp.get("text", "")


def call_tool(name, arguments):
    """Call one bridge tool. Returns an MCP content-block result dict."""
    arguments = arguments or {}
    if name == "read_active_tab":
        resp = call_host("read_active_tab")
        warn = False
    elif name == "open_and_read_url":
        url = arguments.get("url", "")
        if not isinstance(url, str) or not url.lower().startswith(
            ("http://", "https://")
        ):
            return _tool_error("error: 'url' must be an http(s) URL")
        warn = not (load_config().get("allowed_domains") or [])
        if not url_allowed(url):
            return _tool_error(
                "error: domain not in allowed_domains (see %s). "
                "Refusing to open this URL in your logged-in browser."
                % config_path()
            )
        resp = call_host("open_and_read_url", {"url": url})
    else:
        return _tool_error("error: unknown tool '%s'" % name)
    if not resp.get("ok"):
        return _tool_error("error: %s" % resp.get("error", "unknown"))
    text = format_page_result(resp)
    if warn:
        text = empty_allowlist_warning() + text
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def _package_file(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = SERVER_NAME + "/" + __version__

    def log_message(self, fmt, *args):  # keep stdout clean; tests read it
        sys.stderr.write("webmcp-bridge: " + fmt % args + "\n")
        sys.stderr.flush()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, filename, content_type):
        try:
            with open(_package_file(filename), "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json({"ok": True, "version": __version__})
        elif self.path == "/webmcp/tools":
            self._send_json({"tools": TOOLS})
        elif self.path == "/webmcp.js":
            self._send_file("webmcp.js", "application/javascript")
        elif self.path in ("/", "/demo.html"):
            self._send_file("demo.html", "text/html; charset=utf-8")
        else:
            self.send_error(404, "not found")

    def do_POST(self):
        if self.path != "/webmcp/tools/call":
            self.send_error(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json({"error": "empty request body"}, status=400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "request body must be JSON"}, status=400)
            return
        name = body.get("name")
        arguments = body.get("arguments") or {}
        if not name or name not in TOOL_NAMES:
            self._send_json(
                {"error": "unknown tool '%s'" % name}, status=400
            )
            return
        if not isinstance(arguments, dict):
            self._send_json({"error": "'arguments' must be an object"}, status=400)
            return
        result = call_tool(name, arguments)
        status = 200  # tool-level failures surface as isError, MCP style
        self._send_json(result, status=status)


def make_server(port=DEFAULT_PORT):
    return ThreadingHTTPServer(("127.0.0.1", port), BridgeHandler)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="webmcp-bridge",
        description="Expose Browser Buddy's logged-in-browser tools as "
        "WebMCP-callable endpoints.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="localhost port"
    )
    args = parser.parse_args(argv)
    server = make_server(args.port)
    print(
        "webmcp-bridge %s listening on http://127.0.0.1:%d "
        "(demo: http://127.0.0.1:%d/)" % (__version__, args.port, args.port),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
