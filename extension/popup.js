"use strict";
const $ = (id) => document.getElementById(id);

function fmtStatus(s) {
  if (!s) return ["–", ""];
  const when = new Date(s.at).toLocaleString();
  if (s.state === "paused") return ["已暂停（已保存的不会丢）", ""];
  if (s.state === "running") {
    const last = s.beat || s.at;
    const min = Math.floor((Date.now() - last) / 60000);
    if (min >= 10) return [`同步中…但已 ${min} 分钟没有进展，可刷新该网站标签页后重新同步`, "err"];
    return [min >= 1 ? `同步中…（${min} 分钟前有进展）` : "同步中…", ""];
  }
  if (s.state === "error") return [s.error, "err"];
  const r = s.result || {};
  return [`${r.total ?? "?"} 个对话 · 本次 ${r.imported ?? 0}${r.failed ? ` · ${r.failed} 个下载失败（下次重试）` : ""} · ${when}`, r.failed ? "err" : "ok"];
}

async function refresh() {
  const c = await chrome.storage.local.get({ status: {}, log: [], autoSync: true, bgSyncHours: 0, port: 8765, concurrency: 3,
    paused: false });
  $("pause").checked = c.paused;
  $("pause-label").textContent = c.paused ? "已暂停" : "读取中";
  $("paused-banner").hidden = !c.paused;
  $("sync").disabled = c.paused;
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
  $("sync").disabled = !authed || $("pause").checked;
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
  $("sync").disabled = $("pause").checked;
  refresh();
});
$("pause").addEventListener("change", async (e) => {
  const paused = e.target.checked;
  await chrome.storage.local.set({ paused });
  await refresh();
  if (!paused) {  // resume: continue where the sync stopped (already-saved conversations are skipped)
    chrome.runtime.sendMessage({ type: "sync-all", full: false });
    setTimeout(refresh, 800);
  }
});
$("export").addEventListener("click", async () => {
  $("export").disabled = true;
  const r = await chrome.runtime.sendMessage({ type: "request-export" });
  $("export").disabled = false;
  $("export").textContent = r && r.ok ? "已发起，请查收邮件；下载后 ctxport sync 会自动导入" : ((r && r.error) || "发起失败");
});
$("auto").addEventListener("change", (e) => chrome.storage.local.set({ autoSync: e.target.checked }));
$("conc").addEventListener("change", (e) => chrome.storage.local.set({ concurrency: Math.max(1, Math.min(8, Math.floor(+e.target.value) || 3)) }));
$("hours").addEventListener("change", (e) => chrome.storage.local.set({ bgSyncHours: Math.max(0, +e.target.value || 0) }));
$("port").addEventListener("change", async (e) => { await chrome.storage.local.set({ port: +e.target.value || 8765 }); ping(); });
chrome.runtime.onMessage.addListener((m) => { if (m && m.type === "log-updated") refresh(); });

refresh();
ping();
