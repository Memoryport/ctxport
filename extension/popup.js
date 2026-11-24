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
  const c = await chrome.storage.local.get({ status: {}, log: [], autoSync: true, bgSyncHours: 0, port: 8765, concurrency: 3 });
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
  $("conc").value = c.concurrency;
  $("port").value = c.port;
}

async function ping() {
  const r = await chrome.runtime.sendMessage({ type: "ping" });
  const ok = r && r.ok;
  const authed = ok && r.authorized;
  $("dot").className = "dot " + (authed ? "ok" : "err");
  $("srv").textContent = !ok ? "本地服务未运行" : authed ? `本地 ctxport ${r.version}` : `本地 ctxport ${r.version} · 未配对`;
  $("hint").hidden = !!ok;
  $("pair").hidden = !ok || !!authed;
  $("sync").disabled = !authed;
}

$("pairbtn").addEventListener("click", async () => {
  $("pairerr").textContent = "";
  $("pairbtn").disabled = true;
  const r = await chrome.runtime.sendMessage({ type: "pair", code: $("code").value });
  $("pairbtn").disabled = false;
  if (!r || !r.ok) {
    $("pairerr").textContent = (r && r.error) || "配对失败 / pairing failed";
    return;
  }
  $("code").value = "";
  ping();
});

$("sync").addEventListener("click", async () => {
  $("sync").disabled = true;
  $("sync").textContent = "同步中…（可以关闭这个窗口）";
  await chrome.runtime.sendMessage({ type: "sync-all", full: $("full").checked });
  $("sync").textContent = "一键同步全部 / Sync all";
  $("sync").disabled = false;
  refresh();
});
$("auto").addEventListener("change", (e) => chrome.storage.local.set({ autoSync: e.target.checked }));
$("conc").addEventListener("change", (e) => chrome.storage.local.set({ concurrency: Math.max(1, Math.min(8, Math.floor(+e.target.value) || 3)) }));
$("hours").addEventListener("change", (e) => chrome.storage.local.set({ bgSyncHours: Math.max(0, +e.target.value || 0) }));
$("port").addEventListener("change", async (e) => { await chrome.storage.local.set({ port: +e.target.value || 8765 }); ping(); });
chrome.runtime.onMessage.addListener((m) => { if (m && m.type === "log-updated") refresh(); });

refresh();
ping();
