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

async function log(platform, text) {
  const { log } = await cfg();
  log.push(`${new Date().toLocaleTimeString()} [${platform}] ${text}`);
  await chrome.storage.local.set({ log: log.slice(-200) });
  chrome.runtime.sendMessage({ type: "log-updated" }).catch(() => {});
}

async function setStatus(platform, status) {
  const c = await cfg();
  c.status[platform] = Object.assign({ at: Date.now() }, status);
  await chrome.storage.local.set({ status: c.status });
  chrome.runtime.sendMessage({ type: "log-updated" }).catch(() => {});
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

// Gemini: scrape each conversation in one reusable background tab.
async function geminiScrape(list, full) {
  const known = (await server("/known?platform=gemini-web")).known || {};
  const todo = CtxportCore.geminiTodo(list, known, full);
  const fresh = todo.filter((c) => !(c.id in known)).length;
  await log("gemini-web", `${list.length} conversations in sidebar, ${fresh} new, ` +
    `${todo.length - fresh} recent ones re-checked for new messages`);
  if (!todo.length) return { platform: "gemini-web", total: list.length, fetched: 0, imported: 0 };
  const tab = await chrome.tabs.create({ url: "about:blank", active: false });
  let imported = 0, done = 0;
  const buf = [];
  try {
    for (const c of todo) {
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
    }
    if (buf.length) {
      const r = await server("/ingest", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform: "gemini-web", conversations: buf.splice(0) }) });
      imported += r.conversations || 0;
    }
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
  return { platform: "gemini-web", total: list.length, fetched: done, imported };
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
  try {
    const r = await sendToTab(tab.id, { type: "sync", platform, full });
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
  }
}

async function syncAll(full, platforms) {
  const out = [];
  for (const p of platforms || Object.keys(SITES)) out.push(await syncSite(p, full));
  return out;
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
      case "progress":
        await log(msg.platform, msg.text);
        return { ok: true };
      case "gemini-scrape-list":
        return geminiScrape(msg.list || [], !!msg.full);
      case "sync-all":
        return syncAll(!!msg.full, msg.platforms);
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
