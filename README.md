# webmcp-bridge

The first **Browser Buddy → WebMCP** bridge: expose a real, logged-in browser
as callable WebMCP tools.

WebMCP (W3C Community Group draft, Chrome origin trial from M149) lets a web
page declare typed, callable tools via `document.modelContext.registerTool()`
so an in-browser AI agent can call them directly instead of scraping the DOM.
This package is the other half of that loop: a tiny localhost server that maps
[Browser Buddy](https://github.com/hahahahahahahahah6/browser-buddy)'s tools —
which read pages through the user's **real, logged-in Chrome profile** — to
WebMCP-style endpoints, plus a page-side JS shim that registers those tools
with the WebMCP API.

## Differentiation

- **First browser-buddy → WebMCP bridge.** Browser Buddy (listed on
  [awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers)) reads
  your actual browser with its real sessions and cookies; this bridge makes
  that capability callable from any web page through the WebMCP API.
- **vs. the official WebMCP demo** (which only shows toy tools like a "greet"
  function): this is a production bridge to a real logged-in browser — pages
  behind logins, paywalls-with-session, and bot checks become callable tools,
  not just demo buttons.

## Install

```bash
pip install webmcp-bridge
```

Requires Python 3.9+ and the standard library only — no dependencies.
You also need Browser Buddy's native host running (Chrome with the Browser
Buddy extension loaded).

## Usage

Start the bridge server (binds `127.0.0.1` only):

```bash
webmcp-bridge            # listens on http://127.0.0.1:18765
webmcp-bridge --port 9000
```

Open the demo page at `http://127.0.0.1:18765/` — it lists the tools and lets
you call them.

To expose the tools on your own page, load the shim:

```html
<script src="http://127.0.0.1:18765/webmcp.js"></script>
```

It fetches the tool manifests from `/webmcp/tools` and calls
`document.modelContext.registerTool()` for each (falling back to
`navigator.modelContext` on Chrome 146-era preview builds), so an in-browser
agent discovers and calls them directly. Each tool's `execute()` POSTs to
`/webmcp/tools/call`, which forwards to the Browser Buddy native host over
its Unix socket.

### Endpoints

| Method | Path              | Description                                      |
|--------|-------------------|--------------------------------------------------|
| GET    | `/webmcp/tools`   | WebMCP tool manifests (name, title, description, inputSchema, annotations) |
| POST   | `/webmcp/tools/call` | `{"name": ..., "arguments": {...}}` → MCP content-block result |
| GET    | `/healthz`        | `{"ok": true, "version": ...}`                   |
| GET    | `/`               | demo page                                        |
| GET    | `/webmcp.js`      | page-side WebMCP registration shim               |

### Tools exposed

- `read_active_tab` — read the user's active Chrome tab (URL, title, text).
- `open_and_read_url` — open a URL in a background tab with the real profile
  (cookies/sessions), extract readable text, close the tab. Requires an
  `http(s)` `url` argument.

### Config

Optional `~/.config/webmcp-bridge/config.json`:

```json
{
  "allowed_domains": ["example.com"],
  "browser_buddy_socket": "/run/user/1000/browser-buddy/browser-buddy.sock"
}
```

`allowed_domains` restricts which sites a page-side agent may open in your
logged-in browser (subdomains match). Empty/missing means everything is
allowed, with a warning baked into the tool result — set it to lock things
down.

## Security notes

- The server binds `127.0.0.1` only; the page-side shim is same-origin by
  design (bridge URL is localhost).
- `open_and_read_url` opens URLs in your **real, logged-in** browser — treat
  the allowlist as your gate, and never expose the bridge port beyond
  localhost.

## License

MIT — see [LICENSE](LICENSE).
