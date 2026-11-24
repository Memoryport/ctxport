// Node tests for extension/background.js with a fake `chrome` API: `node extension/test/background.test.js`
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function fakeChrome() {
  const data = {};
  const later = (v) => new Promise((r) => setTimeout(() => r(v), Math.random() * 3));  // async like the real one
  const ev = () => ({ addListener() {} });
  return {
    data,
    storage: {
      local: {
        get: async (defaults) => later(JSON.parse(JSON.stringify(Object.assign({}, defaults, data)))),
        set: async (obj) => { await later(); Object.assign(data, JSON.parse(JSON.stringify(obj))); },
      },
      onChanged: ev(),
    },
    runtime: { sendMessage: async () => {}, onMessage: ev(), onInstalled: ev(), onStartup: ev() },
    alarms: { onAlarm: ev(), clear: async () => {}, create() {} },
    tabs: { onUpdated: ev() },
  };
}

function load() {
  const chrome = fakeChrome();
  const ctx = { chrome, console, setTimeout, clearTimeout, fetch: async () => { throw new Error("offline"); } };
  ctx.globalThis = ctx;
  ctx.importScripts = (f) => vm.runInContext(fs.readFileSync(path.join(__dirname, "..", f), "utf8"), ctx);
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8"), ctx);
  return { ctx, chrome };
}

async function testConcurrentLogAndStatus() {
  const { ctx, chrome } = load();
  const jobs = [];
  for (let i = 0; i < 30; i++) {
    jobs.push(ctx.log("chatgpt", "line " + i));
    jobs.push(ctx.setStatus(["chatgpt", "claude-ai", "gemini-web"][i % 3], { state: "running", n: i }));
  }
  await Promise.all(jobs);
  assert.strictEqual(chrome.data.log.length, 30, "no log line is lost");
  assert.deepStrictEqual(Object.keys(chrome.data.status).sort(), ["chatgpt", "claude-ai", "gemini-web"]);
}

async function testClaimAutoSync() {
  const { ctx, chrome } = load();
  const claims = await Promise.all([1, 2, 3].map(() => ctx.claimAutoSync("chatgpt")));
  assert.deepStrictEqual(claims.map((c) => c.ok), [true, false, false], "only one tab may start the auto-sync");
  assert.ok((await ctx.claimAutoSync("claude-ai")).ok, "other sites have their own window");
  assert.ok(chrome.data.lastAuto.chatgpt && chrome.data.lastAuto["claude-ai"]);
  assert.ok(!(await ctx.claimAutoSync("evil")).ok);
}

(async () => {
  await testConcurrentLogAndStatus();
  await testClaimAutoSync();
  console.log("extension background tests passed");
})().catch((e) => { console.error(e); process.exit(1); });
