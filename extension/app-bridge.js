/* Runs only on the local ctxport app page (http://127.0.0.1:<port>/ with <meta name="ctxport-app">).
 * Relays "write my memory into <site>" requests to the extension. Only the target name crosses this bridge;
 * the text itself is fetched by the extension from the paired ctxport receiver. */
(function () {
  "use strict";
  if (!document.querySelector('meta[name="ctxport-app"]')) return;
  const TARGETS = new Set(["chatgpt", "claude-ai", "gemini-web"]);
  const reply = (data) => window.postMessage(Object.assign({ source: "ctxport-extension" }, data), location.origin);
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || ev.origin !== location.origin) return;
    const m = ev.data;
    if (!m || m.source !== "ctxport-app") return;
    if (m.action === "hello") return reply({ type: "hello" });
    if (m.action === "request-export") {
      chrome.runtime.sendMessage({ type: "request-export" }, (r) => {
        const err = chrome.runtime.lastError;
        reply({ id: m.id, ...(err ? { ok: false, error: err.message } : (r || { ok: false, error: "no response" })) });
      });
      return;
    }
    if (m.action === "push-memory" && TARGETS.has(m.target)) {
      chrome.runtime.sendMessage({ type: "push-memory", target: m.target }, (r) => {
        const err = chrome.runtime.lastError;
        reply({ id: m.id, ...(err ? { ok: false, error: err.message } : (r || { ok: false, error: "no response" })) });
      });
    }
  });
  reply({ type: "hello" });
})();
