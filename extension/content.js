/* Runs on chatgpt.com, claude.ai and gemini.google.com. Fetches use the page's own login (same origin). */
(function () {
  "use strict";
  const Core = globalThis.CtxportCore;
  const host = location.hostname;
  const platform = /chatgpt\.com|chat\.openai\.com/.test(host) ? "chatgpt"
    : /claude\.ai/.test(host) ? "claude-ai"
    : /gemini\.google\.com/.test(host) ? "gemini-web" : null;
  if (!platform) return;
  // The background may inject this script into a tab that was open before the extension loaded;
  // never register the listeners twice in one page.
  // After the extension is reloaded, the old copy is still in the page but disconnected (chrome.runtime.id is
  // gone), so only a copy that is still connected counts.
  if (typeof globalThis.__ctxportAlive === "function" && globalThis.__ctxportAlive()) return;
  globalThis.__ctxportAlive = () => { try { return !!chrome.runtime.id; } catch (e) { return false; } };
  let running = false;
  // The pause switch in the popup (chrome.storage "paused"): a running sync stops at its next checkpoint.
  let paused = false;
  chrome.storage.local.get({ paused: false }, (c) => { paused = !!c.paused; });
  chrome.storage.onChanged.addListener((ch) => { if (ch.paused) paused = !!ch.paused.newValue; });

  function bg(msg) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(msg, (r) => {
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
        if (r && r.error) return reject(new Error(r.error));
        resolve(r);
      });
    });
  }

  function makeIO(full, concurrency) {
    return {
      full,
      concurrency,
      shouldStop: () => paused,
      // Timers in a background tab are throttled to about one wake-up per minute, which turned a 30 s
      // rate-limit pause into several minutes. The extension's background worker keeps exact time.
      sleep: (ms) => bg({ type: "sleep", ms }).catch(() => new Promise((r) => setTimeout(r, ms))),
      log: (text) => bg({ type: "progress", platform, text }).catch(() => {}),
      known: async () => (await bg({ type: "known", platform })).known,
      push: (payload) => bg({ type: "ingest", payload }),
      getJSON: async (url, headers) => {
        const res = await fetch(url, { credentials: "include", headers: Object.assign({ Accept: "application/json" }, headers || {}) });
        if (!res.ok) {
          throw Object.assign(new Error(`HTTP ${res.status}`),
            { status: res.status, retryAfter: Number(res.headers.get("retry-after")) || 0 });
        }
        return res.json();
      },
      postJSON: async (url, body, headers) => {
        const res = await fetch(url, { method: "POST", credentials: "include",
          headers: Object.assign({ Accept: "application/json", "Content-Type": "application/json" }, headers || {}),
          body: JSON.stringify(body) });
        if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
        return res.json().catch(() => ({}));
      },
    };
  }

  async function run(full) {
    if (running) return { busy: true };
    running = true;
    try {
      const { concurrency } = await chrome.storage.local.get({ concurrency: Core.DEFAULT_CONCURRENCY });
      if (platform === "chatgpt") return await Core.syncChatGPT(makeIO(full, concurrency));
      if (platform === "claude-ai") return await Core.syncClaude(makeIO(full, concurrency));
      // Gemini: list here, scrape each conversation in a background tab (done by background.js)
      const list = await Core.listGeminiConversations(document, {});
      return await bg({ type: "gemini-scrape-list", list, full });
    } finally {
      running = false;
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "ctxport-hello") {
      sendResponse({ ok: true, platform });
      return false;
    }
    if (msg && msg.type === "sync" && msg.platform === platform) {
      run(!!msg.full).then((r) => sendResponse({ ok: true, result: r }))
        .catch((e) => sendResponse({ ok: false, error: String(e.message || e), status: e.status }));
      return true;
    }
    if (msg && msg.type === "request-export" && platform === "chatgpt") {
      Core.requestChatGPTExport(makeIO(false)).then((r) => sendResponse(r))
        .catch((e) => sendResponse({ ok: false, error: String(e.message || e), status: e.status }));
      return true;
    }
    if (msg && msg.type === "write-memory" && msg.platform === platform) {
      const job = platform === "chatgpt" ? Core.writeChatGPTMemory(makeIO(false), msg.text)
        : Core.fillMemoryForm(document, window, msg.text, {});
      job.then((r) => sendResponse(r)).catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
      return true;
    }
    if (msg && msg.type === "gemini-scrape-here" && platform === "gemini-web") {
      Core.scrapeGeminiConversation(document, window, {}).then((r) => sendResponse({ ok: true, conversation: r }))
        .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
      return true;
    }
    return false;
  });

  // Automatic incremental sync when you open the site (default on, at most every 6 hours).
  // Gemini tabs that ctxport opens for scraping carry #ctxport-scrape and must not start another sync.
  if (location.hash.includes("ctxport-scrape")) sessionStorage.setItem("ctxport-scrape-tab", "1");
  chrome.storage.local.get({ autoSync: true, paused: false }, async (cfg) => {
    if (!cfg.autoSync || cfg.paused) return;
    if (sessionStorage.getItem("ctxport-scrape-tab")) return;
    try {
      const up = await bg({ type: "ping" });
      if (!up || !up.ok || !up.authorized) return;  // receiver not running or not paired: stay quiet
      // The background worker checks and records the 6-hour window in one step, so two open tabs
      // can't both start a sync or overwrite each other's timestamps.
      const claim = await bg({ type: "claim-auto-sync", platform });
      if (!claim || !claim.ok) return;
      await new Promise((r) => setTimeout(r, 4000));  // let the page settle
      const r = await run(false);
      bg({ type: "progress", platform, text: "auto-sync done: " + JSON.stringify(r) }).catch(() => {});
    } catch (e) {
      bg({ type: "progress", platform, text: "auto-sync failed: " + (e.message || e) }).catch(() => {});
    }
  });
})();
