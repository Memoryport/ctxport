/* ctxport companion — background service worker.
 * Talks only to the local ctxport receiver at http://127.0.0.1:<port> (default 8765).
 */
"use strict";

importScripts("core.js");  // CtxportCore (shared with the content script)

const SITES = {
  "chatgpt": { url: "https://chatgpt.com/", match: ["https://chatgpt.com/*", "https://chat.openai.com/*"] },
  "claude-ai": { url: "https://claude.ai/", match: ["https://claude.ai/*"] },
  "gemini-web": { url: "https://gemini.google.com/app", match: ["https://gemini.google.com/*"] },
};

async function cfg() {
  return chrome.storage.local.get({ port: 8765, autoSync: true, bgSyncHours: 0, log: [], status: {} });
}

// Every request carries the token the receiver issued when this extension was paired with a one-time code.
async function server(path, init) {
  const { port } = await cfg();
  const { token } = await chrome.storage.local.get({ token: "" });
  const opts = Object.assign({}, init);
  opts.headers = Object.assign({}, opts.headers, token ? { "X-Ctxport-Token": token } : {});
  const res = await fetch(`http://127.0.0.1:${port}${path}`, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.error || `HTTP ${res.status}`);
    if (body.need_pairing) err.needPairing = true;
    throw err;
  }
  return body;
}

async function pair(code) {
  const r = await server("/pair", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: String(code || "").trim() }) });
  if (!r.token) throw new Error("pairing failed");
  await chrome.storage.local.set({ token: r.token });
  return { ok: true };
}

// `log` and `status` are read-modify-write updates of chrome.storage. Several syncs (and the content scripts'
// progress messages) run at the same time, so the updates go through one queue; otherwise two of them read
// the same old value and the later write drops the other's change.
let storageQueue = Promise.resolve();
function updateStorage(fn) {
  const run = storageQueue.then(fn);
  storageQueue = run.catch(() => {});
  return run;
}

function log(platform, text) {
  return updateStorage(async () => {
    const { log, status } = await cfg();
    log.push(`${new Date().toLocaleTimeString()} [${platform}] ${text}`);
    const update = { log: log.slice(-200) };
    if (status[platform] && status[platform].state === "running") {  // heartbeat, shown in the popup
      status[platform].beat = Date.now();
      update.status = status;
    }
    await chrome.storage.local.set(update);
    chrome.runtime.sendMessage({ type: "log-updated" }).catch(() => {});
  });
}

function setStatus(platform, status) {
  return updateStorage(async () => {
    const c = await cfg();
    c.status[platform] = Object.assign({ at: Date.now() }, status);
    await chrome.storage.local.set({ status: c.status });
    chrome.runtime.sendMessage({ type: "log-updated" }).catch(() => {});
  });
}

const AUTO_SYNC_EVERY = 6 * 3600 * 1000;

// A content script may auto-sync its site at most every 6 hours; returns {ok: true} to the one that may.
function claimAutoSync(platform) {
  return updateStorage(async () => {
    if (!(platform in SITES)) return { ok: false };
    const { lastAuto } = await chrome.storage.local.get({ lastAuto: {} });
    if (Date.now() - (lastAuto[platform] || 0) < AUTO_SYNC_EVERY) return { ok: false };
    lastAuto[platform] = Date.now();
    await chrome.storage.local.set({ lastAuto });
    return { ok: true };
  });
}

function waitTabComplete(tabId, timeout = 45000) {
  return new Promise((resolve) => {
    const t = setTimeout(done, timeout);
    function done() { clearTimeout(t); chrome.tabs.onUpdated.removeListener(on); resolve(); }
    function on(id, info) { if (id === tabId && info.status === "complete") done(); }
    chrome.tabs.onUpdated.addListener(on);
    chrome.tabs.get(tabId).then((tab) => { if (tab.status === "complete") done(); }).catch(done);
  });
}

function sendToTab(tabId, msg, tries = 20) {
  return new Promise((resolve, reject) => {
    const attempt = (n) => chrome.tabs.sendMessage(tabId, msg, (r) => {
      if (chrome.runtime.lastError) {
        if (n <= 0) return reject(new Error(chrome.runtime.lastError.message));
        return setTimeout(() => attempt(n - 1), 500);
      }
      resolve(r);
    });
    attempt(tries);
  });
}

// Exact waits for content scripts (their own timers are throttled while the tab is in the background).
// Touching an extension API every few seconds keeps this service worker alive during the wait.
async function keepAliveSleep(ms) {
  const end = Date.now() + Math.max(0, Math.min(ms, 10 * 60 * 1000));
  while (Date.now() < end) {
    await new Promise((r) => setTimeout(r, Math.min(5000, end - Date.now())));
    await chrome.runtime.getPlatformInfo().catch(() => {});
  }
  return { ok: true };
}

// Gemini: scrape each conversation in one reusable background tab.
async function geminiScrape(list, full) {
  const known = (await server("/known?platform=gemini-web")).known || {};
  const todo = CtxportCore.geminiTodo(list, known, full);
  const fresh = todo.filter((c) => !(c.id in known)).length;
  await log("gemini-web", `${list.length} conversations in sidebar, ${fresh} new, ` +
    `${todo.length - fresh} recent ones re-checked for new messages`);
  if (!todo.length) return { platform: "gemini-web", total: list.length, fetched: 0, imported: 0 };
  // Gemini pages are heavy, so use at most 3 background tabs at once.
  const { concurrency } = await chrome.storage.local.get({ concurrency: CtxportCore.DEFAULT_CONCURRENCY });
  const nTabs = Math.max(1, Math.min(3, CtxportCore.concurrency({ concurrency }), todo.length));
  const tabs = [];
  for (let i = 0; i < nTabs; i++) tabs.push(await chrome.tabs.create({ url: "about:blank", active: false }));
  const free = tabs.map((t) => t.id);
  let imported = 0, done = 0;
  const buf = [];
  try {
    await CtxportCore.pool(todo, nTabs, async (c) => {
      const tab = { id: free.pop() };
      try {
        await chrome.tabs.update(tab.id, { url: `https://gemini.google.com/app/${c.id}#ctxport-scrape` });
        await waitTabComplete(tab.id);
        try {
          const r = await sendToTab(tab.id, { type: "gemini-scrape-here" });
          if (r && r.ok && r.conversation && r.conversation.messages.length) {
            if (!r.conversation.title) r.conversation.title = c.title;
            buf.push(r.conversation);
          } else {
            await log("gemini-web", `no messages found in ${c.id} (page layout may have changed)`);
          }
        } catch (e) {
          await log("gemini-web", `scrape failed for ${c.id}: ${e.message}`);
        }
        done++;
        if (buf.length >= 5) {
          const r = await server("/ingest", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform: "gemini-web", conversations: buf.splice(0) }) });
          imported += r.conversations || 0;
        }
        if (done % 5 === 0) await log("gemini-web", `${done}/${todo.length}`);
      } finally {
        free.push(tab.id);
      }
    });
    if (buf.length) {
      const r = await server("/ingest", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform: "gemini-web", conversations: buf.splice(0) }) });
      imported += r.conversations || 0;
    }
  } finally {
    for (const t of tabs) chrome.tabs.remove(t.id).catch(() => {});
  }
  return { platform: "gemini-web", total: list.length, fetched: done, imported };
}

// A tab that was open before the extension was (re)loaded has no content script, and Edge/Chrome may have put
// an idle tab to sleep (discarded). Wake the tab if needed and inject the scripts, so syncing never fails with
// "Could not establish connection. Receiving end does not exist."
async function ensureContentScript(tabId) {
  let tab = await chrome.tabs.get(tabId);
  if (tab.discarded || tab.status === "unloaded") {
    await chrome.tabs.reload(tabId);
    await waitTabComplete(tabId);
    tab = await chrome.tabs.get(tabId);
  }
  try {
    const r = await chrome.tabs.sendMessage(tabId, { type: "ctxport-hello" });
    if (r && r.ok) return;
  } catch (e) { /* no listener yet: inject below */ }
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["core.js", "content.js"] });
  } catch (e) {
    // e.g. the tab is still loading or showing an error page: reload once and let the manifest inject
    await chrome.tabs.reload(tabId);
    await waitTabComplete(tabId);
  }
}

// Find (or quietly open) a tab of the site and ask its content script to sync.
async function syncSite(platform, full) {
  const site = SITES[platform];
  let tabs = await chrome.tabs.query({ url: site.match });
  tabs = tabs.filter((t) => !(t.url || "").includes("ctxport-scrape"));
  let opened = null;
  let tab = tabs[0];
  if (!tab) {
    opened = tab = await chrome.tabs.create({ url: site.url, active: false });
    await waitTabComplete(tab.id);
  }
  await setStatus(platform, { state: "running" });
  // Edge/Chrome put idle background tabs to sleep, which silently kills a long sync: keep this tab awake
  // while it syncs, and if the tab dies anyway, wake it and continue (already-saved conversations are skipped).
  chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
  try {
    let r;
    for (let attempt = 1; ; attempt++) {
      try {
        await ensureContentScript(tab.id);
        r = await sendToTab(tab.id, { type: "sync", platform, full: full && attempt === 1 });
        break;
      } catch (e) {
        const lost = /port closed|Receiving end|No tab|discard|context invalidated/i.test(String(e && e.message));
        if (!lost || attempt >= 3) throw e;
        await log(platform, `tab was put to sleep or reloaded — resuming (attempt ${attempt + 1})`);
      }
    }
    if (!r || !r.ok) throw Object.assign(new Error((r && r.error) || "no response"), { status: r && r.status });
    await setStatus(platform, { state: "done", result: r.result });
    await log(platform, "done: " + JSON.stringify(r.result));
    return r.result;
  } catch (e) {
    const msg = e.status === 401 || e.status === 403 ? "not logged in — open the site and sign in" : e.message;
    await setStatus(platform, { state: "error", error: msg });
    await log(platform, "error: " + msg);
    return { platform, error: msg };
  } finally {
    if (opened) chrome.tabs.remove(opened.id).catch(() => {});
    else chrome.tabs.update(tab.id, { autoDiscardable: true }).catch(() => {});
  }
}

// The three sites are synced at the same time. Each site limits its own request rate, so running them side
// by side is faster without provoking more 429s; within one site the adaptive limit in core.js applies.
async function syncAll(full, platforms) {
  const list = platforms || Object.keys(SITES);
  const results = await Promise.allSettled(list.map((p) => syncSite(p, full)));
  return results.map((r, i) => (r.status === "fulfilled" ? r.value : { platform: list[i], error: String(r.reason) }));
}

// "一键写入" from the local ctxport app page: the text always comes from the paired ctxport receiver (the user's
// accepted memories), never from the page, so no web page can write its own text into the user's accounts.
const MEMORY_PAGES = {
  "chatgpt": "https://chatgpt.com/",
  "claude-ai": "https://claude.ai/import-memory",
  "gemini-web": "https://gemini.google.com/saved-info",
};

async function pushMemory(target, sender) {
  const from = (sender && (sender.url || (sender.tab && sender.tab.url))) || "";
  if (!/^http:\/\/(127\.0\.0\.1|localhost):\d+\//.test(from)) return { ok: false, error: "only the local ctxport app may ask" };
  if (!MEMORY_PAGES[target]) return { ok: false, error: "unknown target" };
  const { text } = await server(`/memory-text?target=${encodeURIComponent(target)}`);
  if (!text || !text.trim()) return { ok: false, error: "还没有已确认的记忆，先在 ctxport 里审核记忆" };
  let tab;
  if (target === "chatgpt") {
    tab = (await chrome.tabs.query({ url: SITES.chatgpt.match })).find((t) => !(t.url || "").includes("ctxport-scrape"));
  }
  if (!tab) tab = await chrome.tabs.create({ url: MEMORY_PAGES[target], active: target !== "chatgpt" });
  else if (target !== "chatgpt") await chrome.tabs.update(tab.id, { url: MEMORY_PAGES[target], active: true });
  await waitTabComplete(tab.id);
  await ensureContentScript(tab.id);
  const r = await sendToTab(tab.id, { type: "write-memory", platform: target, text });
  await log(target, r && r.ok ? (r.message || "memory written") : "memory write failed: " + ((r && r.error) || "?"));
  return r || { ok: false, error: "no response" };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    switch (msg && msg.type) {
      case "ping":
        try { return await server("/ping"); } catch (e) { return { ok: false, error: e.message }; }
      case "pair":
        if (sender.tab) return { ok: false, error: "pairing is only possible from the popup" };
        try { return await pair(msg.code); } catch (e) { return { ok: false, error: e.message }; }
      case "known":
        return server(`/known?platform=${encodeURIComponent(msg.platform)}`);
      case "ingest":
        return server("/ingest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(msg.payload) });
      case "sleep":
        return keepAliveSleep(Number(msg.ms) || 0);
      case "progress":
        await log(msg.platform, msg.text);
        return { ok: true };
      case "claim-auto-sync":
        return claimAutoSync(msg.platform);
      case "gemini-scrape-list":
        return geminiScrape(msg.list || [], !!msg.full);
      case "sync-all":
        return syncAll(!!msg.full, msg.platforms);
      case "push-memory":
        return pushMemory(msg.target, sender);
      default:
        return { error: "unknown message" };
    }
  })().then(sendResponse, (e) => sendResponse({ error: String(e.message || e) }));
  return true;
});

// Optional periodic background sync (off by default; opens background tabs briefly).
async function scheduleAlarm() {
  const { bgSyncHours } = await cfg();
  await chrome.alarms.clear("ctxport-sync");
  if (bgSyncHours > 0) chrome.alarms.create("ctxport-sync", { periodInMinutes: bgSyncHours * 60 });
}
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== "ctxport-sync") return;
  server("/ping").then((r) => { if (r.authorized) return syncAll(false); }).catch(() => {});
});
chrome.runtime.onInstalled.addListener(scheduleAlarm);
chrome.runtime.onStartup.addListener(scheduleAlarm);
chrome.storage.onChanged.addListener((changes) => { if (changes.bgSyncHours) scheduleAlarm(); });
