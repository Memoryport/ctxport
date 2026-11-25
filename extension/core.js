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
  const DEFAULT_CONCURRENCY = 3;
  const MAX_CONCURRENCY = 8;

  function concurrency(io) {
    const n = Math.floor(Number(io.concurrency) || DEFAULT_CONCURRENCY);
    return Math.max(1, Math.min(MAX_CONCURRENCY, n));
  }

  // Shared state for one sync: when a request is rate-limited (429) every worker pauses until `gate.until`,
  // and the number of parallel downloads (`gate.cur`) is halved; after a run of successes it grows back by one.
  function makeGate(n) {
    return { until: 0, cur: n, max: n, ok: 0 };
  }

  // When one request is rate-limited (429), every parallel worker pauses until `gate.until`.
  const now = (io) => (io.now ? io.now() : Date.now());

  // The pause switch: io.shouldStop() turns true → stop at the next checkpoint (saved data is kept).
  function checkStop(io) {
    if (io && typeof io.shouldStop === "function" && io.shouldStop()) {
      throw Object.assign(new Error("paused"), { fatal: true, paused: true });
    }
  }

  // Waits in short steps so a pause takes effect even during a long rate-limit wait.
  async function sleepChecked(io, ms) {
    const end = now(io) + ms;
    while (now(io) < end) {
      checkStop(io);
      await io.sleep(Math.min(5000, end - now(io)));
    }
    checkStop(io);
  }

  async function waitGate(io, gate) {
    while (gate && gate.until > now(io)) {
      checkStop(io);
      await io.sleep(Math.min(5000, gate.until - now(io)));
    }
  }

  async function withRetry(io, fn, what, gate) {
    let wait = 2000;
    for (let attempt = 0; attempt < 6; attempt++) {
      checkStop(io);
      await waitGate(io, gate);
      try {
        const r = await fn();
        if (gate && ++gate.ok >= 25 && gate.cur < gate.max) { gate.cur++; gate.ok = 0; wakeAll(gate); }
        return r;
      } catch (e) {
        const status = e && e.status;
        if (status === 401 || status === 403 || (e && e.paused)) throw e;
        if (status === 404) return null;
        if (attempt === 5) throw e;
        const retryAfter = Number(e && e.retryAfter) > 0 ? Number(e.retryAfter) * 1000 : 0;
        const pause = status === 429 ? Math.max(wait, 30000, retryAfter) : wait;
        if (status === 429 && gate) {
          gate.until = Math.max(gate.until, now(io) + pause);
          if (gate.cur > 1) {
            gate.cur = Math.max(1, Math.floor(gate.cur / 2));
            io.log(`rate-limited: slowing down to ${gate.cur} parallel download(s)`);
          }
          gate.ok = 0;
        }
        io.log(`${what}: ${status || e.message || e} — retry in ${Math.round(pause / 1000)}s`);
        await sleepChecked(io, pause);
        wait *= 2;
      }
    }
    return null;
  }

  // An error that makes going on pointless: signed out (401/403), or the local ctxport server refused the data.
  const isFatal = (e) => !!e && (e.status === 401 || e.status === 403 || e.fatal === true);

  function wakeAll(gate) {
    const w = gate.wake || [];
    gate.wake = [];
    for (const r of w) r();
  }

  // Run `fn` over `items` with at most `n` in flight (or fewer while `gate.cur` is lowered after a 429).
  // One item that keeps failing is logged and skipped (it is fetched again next sync, since it never reached
  // the inbox), so it cannot abort the whole sync. A fatal error stops every worker, and is rethrown only once
  // all of them have finished, so no worker keeps running after the sync has returned.
  // Returns the number of items that failed.
  async function pool(items, n, fn, gate, io) {
    let next = 0, failed = 0, fatal = null;
    const worker = async (_, w) => {
      while (next < items.length && !fatal) {
        if (gate && w >= gate.cur) {
          // Parked until the limit grows again or the work runs out. A promise, not a polling timer:
          // browsers throttle timers in background tabs to about one per minute.
          await new Promise((r) => (gate.wake = gate.wake || []).push(r));
          continue;
        }
        try {
          checkStop(io);
        } catch (e) {
          fatal = fatal || e;
          if (gate) wakeAll(gate);
          break;
        }
        const i = next++;
        try {
          await fn(items[i], i);
        } catch (e) {
          if (isFatal(e)) { fatal = fatal || e; }
          else {
            failed++;
            if (io && io.log) io.log(`skipped one item after repeated errors: ${(e && (e.status || e.message)) || e}`);
          }
        } finally {
          if (gate && (next >= items.length || fatal)) wakeAll(gate);
        }
        if (fatal) break;
      }
    };
    await Promise.all(Array.from({ length: Math.min(n, items.length) }, worker));
    if (fatal) throw fatal;
    return failed;
  }

  async function flush(io, platform, buf, extra) {
    if (!buf.length && !extra) return { conversations: 0 };
    const payload = Object.assign({ platform, conversations: buf.splice(0, buf.length) }, extra || {});
    try {
      return await io.push(payload);
    } catch (e) {
      throw Object.assign(e instanceof Error ? e : new Error(String(e)), { fatal: true });
    }
  }

  // ------------------------------------------------------------------ ChatGPT
  async function syncChatGPT(io) {
    const session = await io.getJSON("/api/auth/session");
    if (!session || !session.accessToken) throw Object.assign(new Error("not logged in to ChatGPT"), { status: 401 });
    const H = { Authorization: "Bearer " + session.accessToken };
    const N = concurrency(io);
    const gate = makeGate(N);
    const get = (u) => withRetry(io, () => io.getJSON(u, H), u.split("?")[0], gate);
    const known = (await io.known()) || {};

    const items = new Map();
    for (const archived of [false, true]) {
      const url = (offset) => `/backend-api/conversations?offset=${offset}&limit=100&order=updated${archived ? "&is_archived=true" : ""}`;
      const add = (page) => {
        for (const it of (page && page.items) || []) items.set(it.id, Object.assign({ _archived: archived }, it));
      };
      // The `total` in these responses is not reliable (on a real account it stopped a sync at ~200 of 2,200+
      // conversations), so never use it to decide where the list ends: fetch pages in parallel waves and stop
      // only when a page comes back with fewer than 100 items.
      let reported = null;
      for (let offset = 0, more = true; more; ) {
        checkStop(io);
        const offs = Array.from({ length: Math.max(1, gate.cur) }, (_, k) => offset + k * 100);
        const pages = await Promise.all(offs.map((o) => get(url(o))));
        for (const p of pages) {
          add(p);
          if (p && p.total != null) reported = Math.max(reported || 0, p.total);
        }
        more = pages.every((p) => ((p && p.items) || []).length === 100);
        offset += offs.length * 100;
        io.log(`ChatGPT: listed ${items.size} ${archived ? "archived " : ""}conversations…`);
      }
      if (reported != null && reported !== items.size && !archived) {
        io.log(`ChatGPT: server reported total=${reported}, listed ${items.size} (the list walk is authoritative)`);
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
    io.log(`ChatGPT: ${items.size} total, ${todo.length} new or changed, downloading ${N} at a time`);
    const buf = [];
    let done = 0, sent = 0;
    const failed = await pool(todo, N, async (it) => {
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
      if (done % 10 === 0) io.log(`ChatGPT: ${done}/${todo.length}`);
    }, gate, io);
    const extra = {};
    try { const m = await get("/backend-api/memories?include_memory_entries=true"); if (m) extra.memories = m.memories || m.items || []; } catch (e) { /* optional */ }
    try { const ci = await get("/backend-api/user_system_messages"); if (ci) extra.custom_instructions = ci; } catch (e) { /* optional */ }
    const r = await flush(io, "chatgpt", buf, Object.keys(extra).length ? extra : null);
    sent += (r && r.conversations) || 0;
    if (failed) io.log(`ChatGPT: ${failed} conversation(s) could not be downloaded; they are retried next sync`);
    return { platform: "chatgpt", total: items.size, fetched: done, imported: sent, failed };
  }

  // ------------------------------------------------------------------ Claude.ai
  async function syncClaude(io) {
    const N = concurrency(io);
    const gate = makeGate(N);
    const get = (u) => withRetry(io, () => io.getJSON(u), u.split("?")[0], gate);
    const orgs = await get("/api/organizations");
    if (!Array.isArray(orgs) || !orgs.length) throw Object.assign(new Error("not logged in to Claude.ai"), { status: 401 });
    const chatOrgs = orgs.filter((o) => !o.capabilities || o.capabilities.includes("chat"));
    const known = (await io.known()) || {};
    let total = 0, done = 0, sent = 0, failed = 0;
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
      failed += await pool(todo, N, async (c) => {
        const conv = await get(`${base}/chat_conversations/${c.uuid}?tree=True&rendering_mode=messages&render_all_tools=true`);
        done++;
        if (conv) {
          conv._ctxport_stamp = String(c.updated_at);
          if (c.project && !conv.project) conv.project = c.project;
          buf.push(conv);
        }
        if (buf.length >= BATCH) { const r = await flush(io, "claude-ai", buf); sent += r.conversations || 0; }
        if (done % 10 === 0) io.log(`Claude: ${done}/${todo.length}`);
        }, gate, io);
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
      if (Object.keys(extra).length) extra.account = org.uuid;  // stored per organisation
      const r = await flush(io, "claude-ai", buf, Object.keys(extra).length ? extra : null);
      sent += (r && r.conversations) || 0;
    }
    if (failed) io.log(`Claude: ${failed} conversation(s) could not be downloaded; they are retried next sync`);
    return { platform: "claude-ai", total, fetched: done, imported: sent, failed };
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

  // Which Gemini conversations to (re)scrape. The sidebar has no "last updated" stamp, so a chat we already
  // have can't be told apart from one that got new messages. The sidebar is ordered by recent activity, so
  // besides everything new we always re-scrape the `recent` most recent chats, where new messages land.
  const GEMINI_RECENT_REFRESH = 10;
  function geminiTodo(list, known, full, recent) {
    const n = recent == null ? GEMINI_RECENT_REFRESH : recent;
    return list.filter((c, i) => full || !(c.id in (known || {})) || i < n);
  }

  // ------------------------------------------------------------------ writing memory into web AIs
  const CTX_MARK = "[ctxport]";

  // ChatGPT keeps "about you" as one text field (1500 chars). Keep the user's own text, replace only the part
  // from the [ctxport] marker to the end, and shorten ctxport's part (never the user's) to fit the limit.
  function mergeAbout(existing, text, limit) {
    const own = String(existing || "").split(CTX_MARK)[0].replace(/\s+$/, "");
    const sep = own ? "\n\n" : "";
    const room = limit - own.length - sep.length - CTX_MARK.length - 1;
    if (room < 80) return { value: own, fitted: 0, truncated: true };
    let body = String(text || "").trim();
    let truncated = false;
    if (body.length > room) {
      body = body.slice(0, room);
      const cut = body.lastIndexOf("\n");
      if (cut > room * 0.6) body = body.slice(0, cut);
      truncated = true;
    }
    return { value: `${own}${sep}${CTX_MARK}\n${body}`, fitted: body.length, truncated };
  }

  async function writeChatGPTMemory(io, text) {
    const session = await io.getJSON("/api/auth/session");
    if (!session || !session.accessToken) throw Object.assign(new Error("没有登录 ChatGPT"), { status: 401 });
    const H = { Authorization: "Bearer " + session.accessToken };
    const cur = (await io.getJSON("/backend-api/user_system_messages", H)) || {};
    const merged = mergeAbout(cur.about_user_message, text, 1500);
    if (!merged.fitted) throw new Error("ChatGPT“关于你”里你自己写的内容已接近 1500 字上限，没有空间写入");
    await io.postJSON("/backend-api/user_system_messages", Object.assign({}, cur, {
      about_user_message: merged.value,
      about_model_message: cur.about_model_message || "",
      enabled: true,
    }), H);
    return { ok: true, message: `已写入 ChatGPT 自定义指令（关于你）${merged.truncated ? "，因 1500 字上限做了截断" : ""}` };
  }

  // Claude.ai's import-memory page and Gemini's saved-info page are plain forms: put the text into the
  // input and press the page's own add/save button. Experimental: page layouts change.
  function setFieldValue(el, value, win) {
    if (el.isContentEditable) {
      el.focus();
      el.textContent = value;
    } else {
      const proto = el.tagName === "TEXTAREA" ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(el, value);
    }
    el.dispatchEvent(new win.Event("input", { bubbles: true }));
    el.dispatchEvent(new win.Event("change", { bubbles: true }));
  }

  function findButton(doc, re) {
    return [...doc.querySelectorAll("button, [role='button']")].find((b) => {
      const label = `${b.innerText || b.textContent || ""} ${b.getAttribute("aria-label") || ""}`.trim();
      return re.test(label) && !b.disabled && b.getAttribute("aria-disabled") !== "true";
    });
  }

  async function fillMemoryForm(doc, win, text, opts) {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const timeout = (opts && opts.timeout) || 15000;
    const field = () => [...doc.querySelectorAll("textarea, [contenteditable='true'], input[type='text']")]
      .find((el) => !el.disabled && (el.offsetParent !== null || el.getClientRects == null || el.getClientRects().length));
    const t0 = Date.now();
    let el = field();
    while (!el && Date.now() - t0 < timeout) {
      const open = findButton(doc, (opts && opts.openButton) || /^(add|添加|新增|\+)/i);
      if (open) open.click();
      await sleep(600);
      el = field();
    }
    if (!el) throw new Error("页面上找不到输入框（页面可能改版了），请用“复制并打开网页”手动粘贴");
    setFieldValue(el, text, win);
    await sleep(300);
    const submit = findButton(doc, (opts && opts.submitButton) || /(add to memory|添加到记忆|submit|提交|save|保存|^add$|^添加$)/i);
    if (!submit) throw new Error("已填好文本，但找不到提交按钮，请在页面上手动点保存");
    submit.click();
    return { ok: true, message: "已在页面里填好并点击了保存，请在页面上确认结果" };
  }

  // ChatGPT's own "Export data" (Settings → Data controls): the full history arrives by e-mail as a zip, without
  // the per-conversation rate limit. This only presses that button for the logged-in user.
  async function requestChatGPTExport(io) {
    const session = await io.getJSON("/api/auth/session");
    if (!session || !session.accessToken) throw Object.assign(new Error("没有登录 ChatGPT"), { status: 401 });
    await io.postJSON("/backend-api/accounts/data_export", {}, { Authorization: "Bearer " + session.accessToken });
    return { ok: true, message: "已向 ChatGPT 发起官方导出：下载链接会发到你的邮箱。下载后运行 ctxport sync（或在 app 里刷新）会自动导入下载文件夹里的导出包。" };
  }

  root.CtxportCore = { syncChatGPT, syncClaude, scrapeGeminiConversation, listGeminiConversations, geminiTodo,
    GEMINI_RECENT_REFRESH, withRetry, pool, concurrency, makeGate, DEFAULT_CONCURRENCY, checkStop,
    mergeAbout, writeChatGPTMemory, fillMemoryForm, requestChatGPTExport };
})(typeof globalThis !== "undefined" ? globalThis : this);
