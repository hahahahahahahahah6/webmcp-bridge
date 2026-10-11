"""Smoke tests for webmcp-bridge (stdlib only: unittest + threading + http)."""

import http.client
import json
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webmcp_bridge import bridge


class BridgeServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = bridge.make_server(0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = None
        return resp.status, resp.getheader("Content-Type"), payload, raw

    # --- manifests -----------------------------------------------------
    def test_01_manifests_have_webmcp_fields(self):
        for tool in bridge.TOOLS:
            for field in ("name", "title", "description", "inputSchema"):
                self.assertIn(field, tool, "%s missing %s" % (tool["name"], field))
        self.assertEqual({t["name"] for t in bridge.TOOLS},
                         {"read_active_tab", "open_and_read_url"})

    def test_02_manifests_have_annotations(self):
        for tool in bridge.TOOLS:
            ann = tool.get("annotations", {})
            for key in ("readOnlyHint", "untrustedContentHint",
                        "consequentialHint", "debugging"):
                self.assertIn(key, ann)
            self.assertTrue(ann["readOnlyHint"])

    def test_03_open_url_schema_requires_url(self):
        schema = next(t for t in bridge.TOOLS
                      if t["name"] == "open_and_read_url")["inputSchema"]
        self.assertIn("url", schema["required"])
        self.assertEqual(schema["properties"]["url"]["type"], "string")

    # --- HTTP endpoints -------------------------------------------------
    def test_04_healthz(self):
        status, ctype, payload, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        from webmcp_bridge import __version__
        self.assertEqual(payload["version"], __version__)

    def test_05_tools_list_endpoint(self):
        status, ctype, payload, _ = self.request("GET", "/webmcp/tools")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(len(payload["tools"]), 2)

    def test_06_call_unknown_tool_rejected(self):
        status, _, payload, _ = self.request(
            "POST", "/webmcp/tools/call",
            {"name": "nonsense_tool", "arguments": {}})
        self.assertEqual(status, 400)
        self.assertIn("unknown tool", payload["error"])

    def test_07_call_open_url_missing_url(self):
        result = bridge.call_tool("open_and_read_url", {})
        self.assertTrue(result["isError"])
        self.assertIn("'url' must be an http(s) URL", result["content"][0]["text"])

    def test_08_call_open_url_rejects_non_http(self):
        result = bridge.call_tool("open_and_read_url", {"url": "ftp://x.test/a"})
        self.assertTrue(result["isError"])

    def test_09_call_open_url_rejects_javascript(self):
        status, _, payload, _ = self.request(
            "POST", "/webmcp/tools/call",
            {"name": "open_and_read_url", "arguments": {"url": "javascript:alert(1)"}})
        self.assertEqual(status, 200)  # MCP-style: tool error in body
        self.assertTrue(payload["isError"])

    def test_10_non_json_body_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/webmcp/tools/call", body=b"not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)

    def test_11_demo_page_served(self):
        status, ctype, _, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        html = raw.decode("utf-8")
        self.assertIn("document.modelContext", html)
        self.assertIn("webmcp.js", html)

    def test_12_js_shim_served_with_fallbacks(self):
        status, ctype, _, raw = self.request("GET", "/webmcp.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        js = raw.decode("utf-8")
        self.assertIn("document.modelContext", js)
        self.assertIn("navigator.modelContext", js)
        self.assertIn("registerTool", js)

    def test_13_host_unreachable_gives_tool_error(self):
        # force an unreachable socket path without touching global config
        resp = bridge.call_host("read_active_tab",
                                socket_path="/tmp/webmcp-bridge-nope/nonexistent.sock",
                                timeout=2)
        self.assertFalse(resp["ok"])
        self.assertIn("not reachable", resp["error"])

    def test_14_end_to_end_forward_to_fake_host(self):
        sock_path = "/tmp/webmcp-bridge-test.sock"
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        t = threading.Thread(target=_accept_once, args=(server,), daemon=True)
        t.start()

        orig = bridge.browser_buddy_socket_path
        bridge.browser_buddy_socket_path = lambda: sock_path
        try:
            result = bridge.call_tool("read_active_tab", {})
        finally:
            bridge.browser_buddy_socket_path = orig
            server.close()
        t.join(timeout=5)
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("https://example.com/", text)
        self.assertIn("hello from fake host", text)
        try:
            os.unlink(sock_path)
        except OSError:
            pass

    def test_15_404_for_unknown_path(self):
        status, _, _, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)


def _accept_once(server):
    conn, _ = server.accept()
    with conn:
        f = conn.makefile("r", encoding="utf-8")
        req = json.loads(f.readline())
        resp = {"id": 1, "ok": True, "url": "https://example.com/",
                "title": "Example", "text": "hello from fake host",
                "truncated": False}
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
