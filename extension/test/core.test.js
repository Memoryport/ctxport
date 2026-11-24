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

(async () => {
  await testChatGPT();
  await testClaude();
  await testGemini();
  console.log("extension core tests passed");
})().catch((e) => { console.error(e); process.exit(1); });
