/* webmcp-bridge page shim (vanilla JS, no dependencies).
 *
 * Loads this script (or the demo page) on any page and it registers
 * Browser Buddy's real-browser tools with the WebMCP API so an in-browser AI
 * agent can call them directly:
 *
 *   document.modelContext.registerTool({name, title, description,
 *     inputSchema, annotations, execute})
 *
 * Compatible with both the current draft (document.modelContext) and the
 * Chrome 146-era early preview (navigator.modelContext). The execute()
 * callback POSTs to the local webmcp-bridge server, which forwards to the
 * Browser Buddy native host -- the user's real, logged-in Chrome profile.
 *
 * Usage:
 *   <script src="http://127.0.0.1:18765/webmcp.js"></script>
 */
(function () {
  "use strict";

  var BRIDGE = "http://127.0.0.1:18765";

  function modelContext() {
    if (
      typeof document !== "undefined" &&
      document.modelContext &&
      typeof document.modelContext.registerTool === "function"
    ) {
      return document.modelContext; // current WebMCP draft (Oct 2026)
    }
    if (
      typeof navigator !== "undefined" &&
      navigator.modelContext &&
      typeof navigator.modelContext.registerTool === "function"
    ) {
      return navigator.modelContext; // Chrome 146 early preview
    }
    return null;
  }

  function callBridge(tool, args) {
    return fetch(BRIDGE + "/webmcp/tools/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: tool, arguments: args || {} }),
    })
      .then(function (res) {
        return res.json().then(function (body) {
          if (!res.ok) throw new Error(body && body.error ? body.error : "bridge error");
          return body; // {content: [...], isError: bool}
        });
      })
      .then(function (body) {
        var text = (body.content || [])
          .map(function (b) {
            return b.text || "";
          })
          .join("\n");
        if (body.isError) throw new Error(text);
        return { content: [{ type: "text", text: text }] };
      });
  }

  function registerAll() {
    var mc = modelContext();
    if (!mc) {
      console.warn(
        "[webmcp-bridge] No WebMCP API found (tried document.modelContext " +
          "and navigator.modelContext). " +
          "Run Chrome with the WebMCP origin trial / flag enabled."
      );
      return Promise.resolve(0);
    }
    return fetch(BRIDGE + "/webmcp/tools")
      .then(function (res) {
        if (!res.ok) throw new Error("bridge /webmcp/tools -> " + res.status);
        return res.json();
      })
      .then(function (data) {
        var tools = data.tools || [];
        var done = tools.map(function (tool) {
          return mc.registerTool({
            name: tool.name,
            title: tool.title,
            description: tool.description,
            inputSchema: tool.inputSchema,
            annotations: tool.annotations,
            execute: function (args) {
              return callBridge(tool.name, args);
            },
          });
        });
        return Promise.all(done).then(function () {
          console.info(
            "[webmcp-bridge] registered " + tools.length + " WebMCP tool(s): " +
              tools.map(function (t) { return t.name; }).join(", ")
          );
          return tools.length;
        });
      })
      .catch(function (err) {
        console.error("[webmcp-bridge] " + err.message);
        return 0;
      });
  }

  // Expose for tests / manual use; auto-register on DOM ready.
  window.webmcpBridge = { registerAll: registerAll, bridgeUrl: BRIDGE };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", registerAll);
  } else {
    registerAll();
  }
})();
