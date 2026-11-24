"use strict";
const $ = (id) => document.getElementById(id);

function fmtStatus(s) {
  if (!s) return ["–", ""];
  const when = new Date(s.at).toLocaleString();
  if (s.state === "running") return ["同步中…", ""];
  if (s.state === "error") return [s.error, "err"];
  const r = s.result || {};
  return [`${r.total ?? "?"} 个对话 · 本次 ${r.imported ?? 0} · ${when}`, "ok"];
}

async function refresh() {
  const c = await chrome.storage.local.get({ status: {}, log: [], autoSync: true, bgSyncHours: 0, port: 8765 });
  for (const p of ["chatgpt", "claude-ai", "gemini-web"]) {
    const [text, cls] = fmtStatus(c.status[p]);
    const el = $("st-" + p);
    el.textContent = text;
    el.className = "st " + cls;
  }
  $("log").textContent = c.log.slice(-40).join("\n");
  $("log").scrollTop = 1e9;
  $("auto").checked = c.autoSync;
  $("hours").value = c.bgSyncHours;
  $("port").value = c.port;
}

async function ping() {
  const r = await chrome.runtime.sendMessage({ type: "ping" });
  const ok = r && r.ok;
  $("dot").className = "dot " + (ok ? "ok" : "err");
  $("srv").textContent = ok ? `本地 ctxport ${r.version}` : "本地服务未运行";
  $("hint").hidden = !!ok;
  $("sync").disabled = !ok;
}

$("sync").addEventListener("click", async () => {
  $("sync").disabled = true;
  $("sync").textContent = "同步中…（可以关闭这个窗口）";
  await chrome.runtime.sendMessage({ type: "sync-all", full: $("full").checked });
  $("sync").textContent = "一键同步全部 / Sync all";
  $("sync").disabled = false;
  refresh();
});
$("auto").addEventListener("change", (e) => chrome.storage.local.set({ autoSync: e.target.checked }));
$("hours").addEventListener("change", (e) => chrome.storage.local.set({ bgSyncHours: Math.max(0, +e.target.value || 0) }));
$("port").addEventListener("change", async (e) => { await chrome.storage.local.set({ port: +e.target.value || 8765 }); ping(); });
chrome.runtime.onMessage.addListener((m) => { if (m && m.type === "log-updated") refresh(); });

refresh();
ping();
