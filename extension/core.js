/* ctxport companion — platform sync logic (no chrome.* APIs here, so it can be unit-tested in Node).
 *
 * Each sync function receives an `io` object:
 *   io.getJSON(url, headers?)   same-origin fetch with the user's existing login (cookies), returns JSON
 *   io.known()                  {id: stamp} already stored by ctxport (for incremental sync)
 *   io.push(payload)            send {platform, conversations[], memories?, ...} to the local ctxport
 *   io.log(text)                progress line
 *   io.sleep(ms)
 *   io.full                     true = re-download everything
 * Only the user's own data is read, only from the site the user is logged into, and it is only sent
 * to 127.0.0.1 (the local ctxport receiver). Nothing goes anywhere else.
 */
(function (root) {
  "use strict";

  const BATCH = 10;

  async function withRetry(io, fn, what) {
    let wait = 2000;
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        return await fn();
      } catch (e) {
        const status = e && e.status;
        if (status === 401 || status === 403) throw e;
        if (status === 404) return null;
        if (attempt === 5) throw e;
        io.log(`${what}: ${status || e.message || e} — retry in ${Math.round(wait / 1000)}s`);
        await io.sleep(status === 429 ? Math.max(wait, 30000) : wait);
        wait *= 2;
      }
    }
    return null;
  }

  async function flush(io, platform, buf, extra) {
    if (!buf.length && !extra) return { conversations: 0 };
    const payload = Object.assign({ platform, conversations: buf.splice(0, buf.length) }, extra || {});
    return io.push(payload);
  }

  // ------------------------------------------------------------------ ChatGPT
  async function syncChatGPT(io) {
    const session = await io.getJSON("/api/auth/session");
    if (!session || !session.accessToken) throw Object.assign(new Error("not logged in to ChatGPT"), { status: 401 });
    const H = { Authorization: "Bearer " + session.accessToken };
    const get = (u) => withRetry(io, () => io.getJSON(u, H), u.split("?")[0]);
    const known = (await io.known()) || {};

    const items = new Map();
    for (const archived of [false, true]) {
      for (let offset = 0; ; offset += 100) {
        const page = await get(`/backend-api/conversations?offset=${offset}&limit=100&order=updated${archived ? "&is_archived=true" : ""}`);
        const list = (page && page.items) || [];
        for (const it of list) items.set(it.id, Object.assign({ _archived: archived }, it));
        io.log(`ChatGPT: listed ${items.size} conversations…`);
        if (list.length < 100 || (page.total != null && offset + 100 >= page.total)) break;
        await io.sleep(300);
      }
    }
    // conversations inside ChatGPT Projects are not always in the main list
    try {
      const side = await get("/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0");
      for (const g of (side && side.items) || []) {
        const gid = g.gizmo && g.gizmo.gizmo && g.gizmo.gizmo.id;
        if (!gid) continue;
        let cursor = "0";
        while (cursor != null) {
          const page = await get(`/backend-api/gizmos/${gid}/conversations?cursor=${encodeURIComponent(cursor)}`);
          for (const it of (page && page.items) || []) items.set(it.id, Object.assign({ _project: gid }, it));
          cursor = page && page.cursor ? page.cursor : null;
          await io.sleep(300);
        }
      }
    } catch (e) {
      io.log("ChatGPT: project list unavailable (" + (e.status || e.message) + "), continuing");
    }

    const todo = [...items.values()].filter((it) => io.full || known[it.id] !== String(it.update_time));
    io.log(`ChatGPT: ${items.size} total, ${todo.length} new or changed`);
    const buf = [];
    let done = 0, sent = 0;
    for (const it of todo) {
      const conv = await get(`/backend-api/conversation/${it.id}`);
      done++;
      if (conv) {
        conv.conversation_id = conv.conversation_id || it.id;
        conv._ctxport_stamp = String(it.update_time);
        if (it._archived) conv.is_archived = true;
        if (it._project && !conv.gizmo_id) conv.gizmo_id = it._project;
        buf.push(conv);
      }
      if (buf.length >= BATCH) { const r = await flush(io, "chatgpt", buf); sent += r.conversations || 0; }
      if (done % 5 === 0) io.log(`ChatGPT: ${done}/${todo.length}`);
      await io.sleep(400);
    }
    const extra = {};
    try { const m = await get("/backend-api/memories?include_memory_entries=true"); if (m) extra.memories = m.memories || m.items || []; } catch (e) { /* optional */ }
    try { const ci = await get("/backend-api/user_system_messages"); if (ci) extra.custom_instructions = ci; } catch (e) { /* optional */ }
    const r = await flush(io, "chatgpt", buf, Object.keys(extra).length ? extra : null);
    sent += (r && r.conversations) || 0;
    return { platform: "chatgpt", total: items.size, fetched: done, imported: sent };
  }

  // ------------------------------------------------------------------ Claude.ai
  async function syncClaude(io) {
    const get = (u) => withRetry(io, () => io.getJSON(u), u.split("?")[0]);
    const orgs = await get("/api/organizations");
    if (!Array.isArray(orgs) || !orgs.length) throw Object.assign(new Error("not logged in to Claude.ai"), { status: 401 });
    const chatOrgs = orgs.filter((o) => !o.capabilities || o.capabilities.includes("chat"));
    const known = (await io.known()) || {};
    let total = 0, done = 0, sent = 0;
    for (const org of chatOrgs.length ? chatOrgs : orgs) {
      const base = `/api/organizations/${org.uuid}`;
      const seen = new Map();
      for (let offset = 0; ; offset += 100) {
        const page = await get(`${base}/chat_conversations?limit=100&offset=${offset}`);
        const list = Array.isArray(page) ? page : (page && (page.data || page.conversations)) || [];
        const before = seen.size;
        for (const c of list) seen.set(c.uuid, c);
        io.log(`Claude: listed ${seen.size} conversations…`);
        if (list.length < 100 || seen.size === before) break;
        await io.sleep(300);
      }
      total += seen.size;
      const todo = [...seen.values()].filter((c) => io.full || known[c.uuid] !== String(c.updated_at));
      io.log(`Claude (${org.name || org.uuid}): ${seen.size} total, ${todo.length} new or changed`);
      const buf = [];
      for (const c of todo) {
        const conv = await get(`${base}/chat_conversations/${c.uuid}?tree=True&rendering_mode=messages&render_all_tools=true`);
        done++;
        if (conv) {
          conv._ctxport_stamp = String(c.updated_at);
          if (c.project && !conv.project) conv.project = c.project;
          buf.push(conv);
        }
        if (buf.length >= BATCH) { const r = await flush(io, "claude-ai", buf); sent += r.conversations || 0; }
        if (done % 5 === 0) io.log(`Claude: ${done}/${todo.length}`);
        await io.sleep(400);
      }
      const extra = {};
      try {
        const projects = await get(`${base}/projects`);
        const detailed = [];
        for (const p of Array.isArray(projects) ? projects : []) {
          const d = await get(`${base}/projects/${p.uuid}`);
          detailed.push(Object.assign({}, p, d || {}));
          await io.sleep(200);
        }
        if (detailed.length) extra.projects = detailed;
      } catch (e) { /* optional */ }
      try { const m = await get(`${base}/memory`); if (m) extra.memories = m; } catch (e) { /* optional */ }
      const r = await flush(io, "claude-ai", buf, Object.keys(extra).length ? extra : null);
      sent += (r && r.conversations) || 0;
    }
    return { platform: "claude-ai", total, fetched: done, imported: sent };
  }

  // ------------------------------------------------------------------ Gemini (DOM scrape; experimental)
  // Runs inside a gemini.google.com/app/<id> tab. Returns {id, title, url, messages[]}.
  async function scrapeGeminiConversation(doc, win, opts) {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const timeout = (opts && opts.timeout) || 25000;
    const t0 = Date.now();
    const turns = () => doc.querySelectorAll("user-query, model-response");
    while (!turns().length && Date.now() - t0 < timeout) await sleep(500);
    // older turns load when scrolling up; scroll until the count is stable
    let stable = 0, last = -1;
    while (stable < 3 && Date.now() - t0 < timeout * 3) {
      const scroller = doc.querySelector("infinite-scroller, .chat-history, [data-test-id='chat-history-container']");
      if (scroller) scroller.scrollTop = 0; else win.scrollTo(0, 0);
      await sleep(900);
      const n = turns().length;
      stable = n === last ? stable + 1 : 0;
      last = n;
    }
    const clean = (el) => (el ? (el.innerText || el.textContent || "").replace(/\n{3,}/g, "\n\n").trim() : "");
    const messages = [];
    for (const el of turns()) {
      if (el.tagName.toLowerCase() === "user-query") {
        const q = el.querySelector(".query-text, [class*='query-text']") || el;
        const files = [...el.querySelectorAll("img, [data-test-id*='file'], .file-name")]
          .map((f) => ({ name: f.getAttribute("alt") || f.getAttribute("title") || clean(f) || null }))
          .filter((f) => f.name);
        messages.push({ role: "user", text: clean(q).replace(/^You said\s*/i, ""), attachments: files.length ? files : undefined });
      } else {
        const thoughts = el.querySelector("model-thoughts");
        if (thoughts && clean(thoughts)) messages.push({ role: "model", kind: "reasoning", text: clean(thoughts) });
        const body = el.querySelector("message-content, .markdown, .model-response-text") || el;
        messages.push({ role: "model", text: clean(body) });
      }
    }
    const id = (win.location.pathname.match(/\/app\/([A-Za-z0-9_-]+)/) || [])[1] || win.location.pathname;
    const title = (doc.querySelector("[data-test-id='conversation-title'], .conversation-title.selected, .selected .conversation-title") || {}).innerText
      || doc.title.replace(/\s*[-|]\s*Gemini.*$/i, "");
    return { id, title: (title || "").trim(), url: win.location.href, scrapedAt: new Date().toISOString(), messages };
  }

  // Runs on any gemini.google.com page: expands the sidebar and returns [{id, title}].
  async function listGeminiConversations(doc, opts) {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const seen = new Map();
    let stable = 0;
    const deadline = Date.now() + ((opts && opts.timeout) || 60000);
    while (stable < 4 && Date.now() < deadline) {
      const before = seen.size;
      for (const a of doc.querySelectorAll("a[href*='/app/']")) {
        const m = (a.getAttribute("href") || "").match(/\/app\/([A-Za-z0-9_-]{6,})/);
        if (m && !seen.has(m[1])) seen.set(m[1], { id: m[1], title: (a.innerText || "").trim() });
      }
      for (const b of doc.querySelectorAll("button, [role='button']")) {
        if (/^(show more|显示更多|更多)$/i.test((b.innerText || "").trim())) b.click();
      }
      const list = doc.querySelector("conversations-list, .conversations-container, side-navigation-content nav, nav");
      if (list) list.scrollTop = list.scrollHeight;
      await sleep(800);
      stable = seen.size === before ? stable + 1 : 0;
    }
    return [...seen.values()];
  }

  root.CtxportCore = { syncChatGPT, syncClaude, scrapeGeminiConversation, listGeminiConversations, withRetry };
})(typeof globalThis !== "undefined" ? globalThis : this);
