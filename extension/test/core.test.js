// Node unit tests for extension/core.js (no browser needed): `node extension/test/core.test.js`
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ctx = { globalThis: {}, setTimeout, console };
ctx.globalThis = ctx;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "..", "core.js"), "utf8"), ctx);
const Core = ctx.CtxportCore;

function makeIO(routes, known, full) {
  const pushed = [];
  const calls = [];
  return {
    pushed, calls, full: !!full,
    sleep: async () => {},
    log: () => {},
    known: async () => known || {},
    push: async (p) => { pushed.push(JSON.parse(JSON.stringify(p))); return { conversations: p.conversations.length }; },
    getJSON: async (url, headers) => {
      calls.push({ url, headers });
      for (const [re, fn] of routes) {
        const m = url.match(re);
        if (m) {
          const r = typeof fn === "function" ? fn(m, headers) : fn;
          if (r instanceof Error) throw r;
          return r;
        }
      }
      throw Object.assign(new Error("404 " + url), { status: 404 });
    },
  };
}

async function testChatGPT() {
  const conv = (id) => ({ title: "t" + id, mapping: {}, current_node: null, update_time: 1 });
  let flaky = 1;
  const routes = [
    [/^\/api\/auth\/session$/, { accessToken: "TOKEN" }],
    [/^\/backend-api\/conversations\?offset=0&limit=100&order=updated$/, { items: [{ id: "a", update_time: "2026-01-01" }, { id: "b", update_time: "2026-01-02" }], total: 2 }],
    [/is_archived=true/, { items: [{ id: "z", update_time: "2025-01-01" }], total: 1 }],
    [/gizmos\/snorlax\/sidebar/, { items: [{ gizmo: { gizmo: { id: "g-p-1" } } }] }],
    [/gizmos\/g-p-1\/conversations/, { items: [{ id: "p1", update_time: "2026-02-01" }], cursor: null }],
    [/^\/backend-api\/conversation\/b$/, () => (flaky-- > 0 ? Object.assign(new Error("rate"), { status: 500 }) : conv("b"))],
    [/^\/backend-api\/conversation\/(\w+)$/, (m) => conv(m[1])],
    [/memories/, { memories: [{ content: "likes tea" }] }],
    [/user_system_messages/, { about_user_message: "I study physics" }],
  ];
  const io = makeIO(routes, { a: "2026-01-01" });
  const r = await Core.syncChatGPT(io);
  assert.strictEqual(r.total, 4);
  assert.strictEqual(r.fetched, 3, "unchanged conversation 'a' is skipped");
  const all = io.pushed.flatMap((p) => p.conversations);
  assert.deepStrictEqual(all.map((c) => c.conversation_id).sort(), ["b", "p1", "z"]);
  assert.ok(all.find((c) => c.conversation_id === "z").is_archived);
  assert.strictEqual(all.find((c) => c.conversation_id === "p1").gizmo_id, "g-p-1");
  assert.strictEqual(all.find((c) => c.conversation_id === "b")._ctxport_stamp, "2026-01-02");
  const last = io.pushed[io.pushed.length - 1];
  assert.deepStrictEqual(last.memories, [{ content: "likes tea" }]);
  assert.strictEqual(last.custom_instructions.about_user_message, "I study physics");
  assert.ok(io.calls.filter((c) => c.url.startsWith("/backend-api")).every((c) => c.headers.Authorization === "Bearer TOKEN"));
  // full resync fetches everything
  const io2 = makeIO(routes, { a: "2026-01-01", b: "2026-01-02" }, true);
  assert.strictEqual((await Core.syncChatGPT(io2)).fetched, 4);
  // logged out
  const io3 = makeIO([[/session/, {}]]);
  await assert.rejects(Core.syncChatGPT(io3), /not logged in/);
}

async function testClaude() {
  const routes = [
    [/^\/api\/organizations$/, [{ uuid: "o1", name: "Personal", capabilities: ["chat"] }, { uuid: "o2", capabilities: ["api"] }]],
    [/chat_conversations\?limit=100&offset=0$/, [{ uuid: "c1", updated_at: "x1" }, { uuid: "c2", updated_at: "x2" }]],
    [/chat_conversations\/c(\d)\?tree=True/, (m) => ({ uuid: "c" + m[1], name: "n", chat_messages: [] })],
    [/\/projects$/, [{ uuid: "p1", name: "Thesis" }]],
    [/\/projects\/p1$/, { prompt_template: "cite sources" }],
    [/\/memory$/, Object.assign(new Error("nf"), { status: 404 })],
  ];
  const io = makeIO(routes, { c1: "x1" });
  const r = await Core.syncClaude(io);
  assert.strictEqual(r.total, 2);
  assert.strictEqual(r.fetched, 1);
  const all = io.pushed.flatMap((p) => p.conversations);
  assert.deepStrictEqual(all.map((c) => c.uuid), ["c2"]);
  assert.strictEqual(all[0]._ctxport_stamp, "x2");
  assert.ok(!io.calls.some((c) => c.url.includes("/o2/")), "non-chat orgs skipped");
  assert.strictEqual(io.pushed[io.pushed.length - 1].projects[0].prompt_template, "cite sources");
  assert.strictEqual(io.pushed[io.pushed.length - 1].account, "o1", "account data is tagged with its organisation");
}

function testGeminiTodo() {
  const list = Array.from({ length: 15 }, (_, i) => ({ id: "g" + i }));
  const known = Object.fromEntries(list.map((c) => [c.id, 3]));
  delete known.g12;  // a new chat further down
  const ids = (cs) => cs.map((c) => c.id);
  assert.deepStrictEqual(ids(Core.geminiTodo(list, known, false, 2)), ["g0", "g1", "g12"],
    "recent chats are re-checked even when known, plus anything new");
  assert.strictEqual(Core.geminiTodo(list, known, false).length, Core.GEMINI_RECENT_REFRESH + 1);
  assert.strictEqual(Core.geminiTodo(list, known, true, 2).length, 15, "full sync takes everything");
  assert.strictEqual(Core.geminiTodo(list, {}, false, 0).length, 15);
}

function el(tag, text, children, attrs) {
  return {
    tagName: tag.toUpperCase(), innerText: text, textContent: text, children: children || [],
    getAttribute: (k) => (attrs || {})[k] || null,
    querySelector(sel) { return find(this, sel)[0] || null; },
    querySelectorAll(sel) { return find(this, sel); },
    click() {},
  };
}
function find(node, sel) {
  const names = sel.split(",").map((s) => s.trim());
  const out = [];
  (function walk(n) {
    for (const c of n.children) {
      const t = c.tagName.toLowerCase();
      if (names.some((s) => s === t || (s.startsWith(".") && (c.cls || []).includes(s.slice(1))) ||
          (s.startsWith("a[href") && t === "a"))) out.push(c);
      walk(c);
    }
  })(node);
  return out;
}

async function testGemini() {
  const q1 = el("user-query", "", [Object.assign(el("div", "What is a prime?"), { cls: ["query-text"] })]);
  const r1 = el("model-response", "", [el("model-thoughts", "thinking about primes"), el("message-content", "A prime has two divisors.")]);
  const q2 = el("user-query", "", [Object.assign(el("div", "Example?"), { cls: ["query-text"] })]);
  const r2 = el("model-response", "", [el("message-content", "7")]);
  const doc = el("html", "", [q1, r1, q2, r2]);
  doc.title = "Primes - Gemini";
  const win = { location: { pathname: "/app/abc123def", href: "https://gemini.google.com/app/abc123def" }, scrollTo() {} };
  const c = await Core.scrapeGeminiConversation(doc, win, { timeout: 50 });
  assert.strictEqual(c.id, "abc123def");
  assert.strictEqual(c.title, "Primes");
  assert.deepStrictEqual(JSON.parse(JSON.stringify(c.messages.map((m) => [m.role, m.kind || "message", m.text]))), [
    ["user", "message", "What is a prime?"], ["model", "reasoning", "thinking about primes"],
    ["model", "message", "A prime has two divisors."], ["user", "message", "Example?"], ["model", "message", "7"]]);
  const side = el("nav", "", [el("a", "Primes", [], { href: "/app/abc123def" }), el("a", "Other", [], { href: "/app/zzz999yyy" })]);
  const list = await Core.listGeminiConversations(el("html", "", [side]), { timeout: 100 });
  assert.deepStrictEqual(JSON.parse(JSON.stringify(list.map((x) => x.id))), ["abc123def", "zzz999yyy"]);
}

async function testParallel() {
  // pool keeps at most n tasks in flight and runs every item once
  let inFlight = 0, peak = 0;
  const seen = [];
  await Core.pool([1, 2, 3, 4, 5, 6, 7], 3, async (x) => {
    inFlight++; peak = Math.max(peak, inFlight);
    await new Promise((r) => setTimeout(r, 5));
    seen.push(x); inFlight--;
  });
  assert.strictEqual(peak, 3);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(seen.sort())), [1, 2, 3, 4, 5, 6, 7]);
  assert.strictEqual(Core.concurrency({}), Core.DEFAULT_CONCURRENCY);
  assert.strictEqual(Core.concurrency({ concurrency: 99 }), 8);
  assert.strictEqual(Core.concurrency({ concurrency: 0 }), Core.DEFAULT_CONCURRENCY);

  // ChatGPT: 250 conversations, total known -> remaining list pages fetched in parallel, details concurrently,
  // and a 429 on one request pauses everyone via the shared gate (io.sleep records the pause)
  const all = Array.from({ length: 250 }, (_, i) => ({ id: "c" + i, update_time: "t" + i }));
  let hit429 = false, active = 0, maxActive = 0;
  const routes = [
    [/^\/api\/auth\/session$/, { accessToken: "T" }],
    [/conversations\?offset=(\d+)&limit=100&order=updated$/, (m) => ({ items: all.slice(+m[1], +m[1] + 100), total: 250 })],
    [/is_archived=true/, { items: [], total: 0 }],
    [/gizmos\/snorlax/, { items: [] }],
    [/^\/backend-api\/conversation\/(c\d+)$/, (m) => {
      if (m[1] === "c7" && !hit429) { hit429 = true; return Object.assign(new Error("slow down"), { status: 429 }); }
      return { title: m[1], mapping: {} };
    }],
    [/memories|user_system_messages/, null],
  ];
  const io = makeIO(routes, {});
  io.concurrency = 5;
  const logs = [];
  io.log = (t) => logs.push(t);
  const sleeps = [];
  let clock = 1e12;  // fake clock: sleeping advances it instantly
  io.now = () => clock;
  io.sleep = async (ms) => {  // instant, but yields like a real timer so parked workers don't spin
    if (ms >= 5000) sleeps.push(ms);
    clock += ms;
    await new Promise((r) => setImmediate(r));
  };
  const get = io.getJSON;
  io.getJSON = async (u, h) => {
    active++; maxActive = Math.max(maxActive, active);
    await new Promise((r) => setTimeout(r, 1));
    try { return await get(u, h); } finally { active--; }
  };
  const r = await Core.syncChatGPT(io);
  assert.strictEqual(r.total, 250);
  assert.strictEqual(r.fetched, 250);
  assert.strictEqual(r.imported, 250, "the rate-limited conversation is retried, none lost");
  assert.ok(maxActive > 1 && maxActive <= 5, "parallel but capped: " + maxActive);
  assert.ok(sleeps.some((ms) => ms >= 5000), "429 triggers a pause");
  assert.ok(logs.some((t) => /slowing down to 2 parallel/.test(t)), "429 halves the parallel downloads");

  // the server's `total` can be wrong (seen on a real account): the list must still be walked to the end
  const many = Array.from({ length: 450 }, (_, i) => ({ id: "m" + i, update_time: "u" + i }));
  const lying = [
    [/^\/api\/auth\/session$/, { accessToken: "T" }],
    [/conversations\?offset=(\d+)&limit=100&order=updated$/, (m) => ({ items: many.slice(+m[1], +m[1] + 100), total: 150 })],
    [/is_archived=true/, { items: [], total: 0 }],
    [/gizmos\/snorlax/, { items: [] }],
    [/^\/backend-api\/conversation\/(m\d+)$/, (m) => ({ title: m[1], mapping: {} })],
    [/memories|user_system_messages/, null],
  ];
  const io3 = makeIO(lying, {});
  io3.concurrency = 3;
  io3.sleep = async () => { await new Promise((r) => setImmediate(r)); };
  const r3 = await Core.syncChatGPT(io3);
  assert.strictEqual(r3.total, 450, "listed every conversation despite total=150");
  assert.strictEqual(r3.imported, 450);

  // Retry-After from the server is honoured when it asks for longer than the default pause
  const gate = Core.makeGate(3);
  const slept = [];
  let first = true;
  let t2 = 0;
  const io2 = { log() {}, now: () => t2, sleep: async (ms) => { slept.push(ms); t2 += ms; } };
  const v = await Core.withRetry(io2, async () => {
    if (first) { first = false; throw Object.assign(new Error("429"), { status: 429, retryAfter: 90 }); }
    return "ok";
  }, "x", gate);
  assert.strictEqual(v, "ok");
  assert.ok(slept.includes(90000), JSON.stringify(slept));
  assert.strictEqual(gate.cur, 1);
}

(async () => {
  await testParallel();
  await testChatGPT();
  await testClaude();
  testGeminiTodo();
  await testGemini();
  console.log("extension core tests passed");
})().catch((e) => { console.error(e); process.exit(1); });
