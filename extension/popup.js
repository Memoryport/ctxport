"use strict";
const $ = (id) => document.getElementById(id);

// Language: Chinese is the source text; English comes from POPUP_EN (static text) and L(zh, en) (text built here).
let EN = !/^zh\b/i.test(navigator.language || "");
const L = (zh, en) => (EN ? en : zh);
const POPUP_EN = {
  "读取中": "Reading",
  "暂停后不会读取任何网站；正在进行的同步会停下，已保存的不会丢": "While paused no site is read; a running sync stops and nothing saved is lost",
  "检查本地服务…": "Checking local service…",
  "已暂停读取：不会自动同步，也不会读取任何网站。打开右上角开关即可继续。": "Reading is paused: no automatic sync and no site is read. Turn the switch at the top right back on to continue.",
  "(实验)": "(experimental)",
  "首次使用需要配对：在终端执行": "First time? Pair it: run",
  "，把显示的配对码填在下面。": "in a terminal and enter the code it prints below.",
  "First time: run": "", "and enter the code it prints.": "",
  "配对 / Pair": "Pair",
  "一键同步全部 / Sync all": "Sync all",
  "ChatGPT 对话太多？一键发起官方导出（邮件发链接）": "Lots of ChatGPT chats? Request the official export (link by email)",
  "全量重新下载（默认只同步新增和改动）": "Download everything again (default: only new and changed chats)",
  "打开这些网站时自动同步（最多每 6 小时一次）": "Sync automatically when you open these sites (at most every 6 hours)",
  "后台定时同步每": "Sync in the background every", "小时（0 = 关闭）": "hours (0 = off)",
  "同时下载": "Download", "个对话（越大越快，太大可能被网站限流）": "chats at once (higher is faster; too high may be rate-limited)",
  "本地端口": "Local port",
  "本地服务未运行。在终端执行": "The local service isn't running. Run",
  "，或用": ", or use", "让它开机自启。": "to start it at login.",
};
function translatePopup() {
  document.documentElement.lang = EN ? "en" : "zh";
  $("lang").textContent = EN ? "中文" : "EN";
  if (!EN) return;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  for (let n = w.nextNode(); n; n = w.nextNode()) nodes.push(n);
  for (const n of nodes) {
    const key = n.nodeValue.trim();
    if (key in POPUP_EN) n.nodeValue = POPUP_EN[key] ? n.nodeValue.replace(key, POPUP_EN[key]) : "";
  }
  // the bilingual first-time hint: drop its English duplicate line
  const br = document.querySelector("#pair .hint br");
  if (br) { while (br.nextSibling) br.nextSibling.remove(); br.remove(); }
  for (const el of document.querySelectorAll("[title]")) {
    const v = el.getAttribute("title");
    if (POPUP_EN[v]) el.setAttribute("title", POPUP_EN[v]);
  }
}

function fmtStatus(s) {
  if (!s) return ["–", ""];
  const when = new Date(s.at).toLocaleString();
  if (s.state === "paused") return [L("已暂停（已保存的不会丢）", "Paused (nothing saved is lost)"), ""];
  if (s.state === "running") {
    const last = s.beat || s.at;
    const min = Math.floor((Date.now() - last) / 60000);
    if (min >= 10) return [L(`同步中…但已 ${min} 分钟没有进展，可刷新该网站标签页后重新同步`, `Syncing… but no progress for ${min} minutes; reload that site's tab and sync again`), "err"];
    return [min >= 1 ? L(`同步中…（${min} 分钟前有进展）`, `Syncing… (last progress ${min} min ago)`) : L("同步中…", "Syncing…"), ""];
  }
  if (s.state === "error") return [s.error, "err"];
  const r = s.result || {};
  return [L(`${r.total ?? "?"} 个对话 · 本次 ${r.imported ?? 0}${r.failed ? ` · ${r.failed} 个下载失败（下次重试）` : ""} · ${when}`,
    `${r.total ?? "?"} chats · ${r.imported ?? 0} new${r.failed ? ` · ${r.failed} failed (retried next time)` : ""} · ${when}`), r.failed ? "err" : "ok"];
}

async function refresh() {
  const c = await chrome.storage.local.get({ status: {}, log: [], autoSync: true, bgSyncHours: 0, port: 8765, concurrency: 3,
    paused: false });
  $("pause").checked = c.paused;
  $("pause-label").textContent = c.paused ? L("已暂停", "Paused") : L("读取中", "Reading");
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
  $("srv").textContent = !ok ? L("本地服务未运行", "Local service not running") : authed ? L(`本地 ctxport ${r.version}`, `Local ctxport ${r.version}`) : L(`本地 ctxport ${r.version} · 未配对`, `Local ctxport ${r.version} · not paired`);
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
    $("pairerr").textContent = (r && r.error) || L("配对失败", "Pairing failed");
    return;
  }
  $("code").value = "";
  ping();
});

$("sync").addEventListener("click", async () => {
  $("sync").disabled = true;
  $("sync").textContent = L("同步中…（可以关闭这个窗口）", "Syncing… (you can close this window)");
  await chrome.runtime.sendMessage({ type: "sync-all", full: $("full").checked });
  $("sync").textContent = L("一键同步全部 / Sync all", "Sync all");
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
  $("export").textContent = r && r.ok ? L("已发起，请查收邮件；下载后 ctxport sync 会自动导入", "Requested; check your email. Once downloaded, ctxport sync imports it") : ((r && r.error) || L("发起失败", "Request failed"));
});
$("auto").addEventListener("change", (e) => chrome.storage.local.set({ autoSync: e.target.checked }));
$("conc").addEventListener("change", (e) => chrome.storage.local.set({ concurrency: Math.max(1, Math.min(8, Math.floor(+e.target.value) || 3)) }));
$("hours").addEventListener("change", (e) => chrome.storage.local.set({ bgSyncHours: Math.max(0, +e.target.value || 0) }));
$("port").addEventListener("change", async (e) => { await chrome.storage.local.set({ port: +e.target.value || 8765 }); ping(); });
chrome.runtime.onMessage.addListener((m) => { if (m && m.type === "log-updated") refresh(); });

$("lang").addEventListener("click", async () => {
  await chrome.storage.local.set({ lang: EN ? "zh" : "en" });
  location.reload();
});

chrome.storage.local.get({ lang: "" }).then(({ lang }) => {
  if (lang === "zh" || lang === "en") EN = lang === "en";
  translatePopup();
  refresh();
  ping();
});
