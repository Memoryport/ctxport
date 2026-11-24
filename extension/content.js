/* Runs on chatgpt.com, claude.ai and gemini.google.com. Fetches use the page's own login (same origin). */
(function () {
  "use strict";
  const Core = globalThis.CtxportCore;
  const host = location.hostname;
  const platform = /chatgpt\.com|chat\.openai\.com/.test(host) ? "chatgpt"
    : /claude\.ai/.test(host) ? "claude-ai"
    : /gemini\.google\.com/.test(host) ? "gemini-web" : null;
  if (!platform) return;
  let running = false;

  function bg(msg) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(msg, (r) => {
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
        if (r && r.error) return reject(new Error(r.error));
        resolve(r);
      });
    });
  }

  function makeIO(full) {
    return {
      full,
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      log: (text) => bg({ type: "progress", platform, text }).catch(() => {}),
      known: async () => (await bg({ type: "known", platform })).known,
      push: (payload) => bg({ type: "ingest", payload }),
      getJSON: async (url, headers) => {
        const res = await fetch(url, { credentials: "include", headers: Object.assign({ Accept: "application/json" }, headers || {}) });
        if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
        return res.json();
      },
    };
  }

  async function run(full) {
    if (running) return { busy: true };
    running = true;
    try {
      if (platform === "chatgpt") return await Core.syncChatGPT(makeIO(full));
      if (platform === "claude-ai") return await Core.syncClaude(makeIO(full));
      // Gemini: list here, scrape each conversation in a background tab (done by background.js)
      const list = await Core.listGeminiConversations(document, {});
      return await bg({ type: "gemini-scrape-list", list, full });
    } finally {
      running = false;
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "sync" && msg.platform === platform) {
      run(!!msg.full).then((r) => sendResponse({ ok: true, result: r }))
        .catch((e) => sendResponse({ ok: false, error: String(e.message || e), status: e.status }));
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
  chrome.storage.local.get({ autoSync: true, lastAuto: {} }, async (cfg) => {
    if (!cfg.autoSync) return;
    if (sessionStorage.getItem("ctxport-scrape-tab")) return;
    const last = cfg.lastAuto[platform] || 0;
    if (Date.now() - last < 6 * 3600 * 1000) return;
    try {
      const up = await bg({ type: "ping" });
      if (!up || !up.ok || !up.authorized) return;  // receiver not running or not paired: stay quiet
      cfg.lastAuto[platform] = Date.now();
      chrome.storage.local.set({ lastAuto: cfg.lastAuto });
      await new Promise((r) => setTimeout(r, 4000));  // let the page settle
      const r = await run(false);
      bg({ type: "progress", platform, text: "auto-sync done: " + JSON.stringify(r) }).catch(() => {});
    } catch (e) {
      bg({ type: "progress", platform, text: "auto-sync failed: " + (e.message || e) }).catch(() => {});
    }
  });
})();
