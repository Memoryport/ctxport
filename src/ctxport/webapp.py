"""`ctxport app`: a local web app to review memories and import them into other AIs with one click.

Security model (it can write other apps' config files, so it is locked down):
* listens on 127.0.0.1 only;
* every /api request must carry the per-launch random token (X-Ctxport-App header) that `ctxport app`
  puts in the URL it opens; the token is never stored on disk;
* the Host header must be 127.0.0.1/localhost on our port (defeats DNS rebinding) and any Origin header
  must be this app's own origin (defeats cross-site requests from other pages).
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .store import Store, default_home, ensure_private_dir, private_write_text

AGENT_TARGETS = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "gemini-cli": "Gemini CLI",
    "antigravity": "Antigravity",
    "cursor": "Cursor",
}
WEB_TARGETS = {
    "chatgpt": {"label": "ChatGPT", "where": "自定义指令（关于你）", "url": "https://chatgpt.com/",
                "limit": 1500},
    "claude-ai": {"label": "Claude.ai", "where": "记忆导入", "url": "https://claude.ai/import-memory",
                  "limit": None},
    "gemini-web": {"label": "Gemini", "where": "已保存的信息", "url": "https://gemini.google.com/saved-info",
                   "limit": None},
}
APP_TARGETS = {
    "chatbox": {"label": "Chatbox", "app": "Chatbox"},
    "cherry-studio": {"label": "Cherry Studio", "app": "Cherry Studio"},
}
MAX_BODY = 2 * 1024 * 1024


def exports_dir() -> Path:
    return default_home() / "exports"


# ---------------------------------------------------------------- text for paste / web memory

SIZES = ("compact", "standard", "full")


def _prefs_file() -> Path:
    return default_home() / "app-prefs.json"


def profile_size_pref() -> str:
    """The profile size the user picked in the app (also used by the extension's one-click write)."""
    try:
        v = json.loads(_prefs_file().read_text(encoding="utf-8")).get("profile_size")
    except (OSError, ValueError, AttributeError):
        v = None
    return v if v in SIZES else "compact"


def set_profile_size_pref(size: str) -> dict:
    if size not in SIZES:
        raise ValueError(f"unknown size {size}")
    try:
        prefs = json.loads(_prefs_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prefs = {}
    prefs["profile_size"] = size
    ensure_private_dir(default_home())
    private_write_text(_prefs_file(), json.dumps(prefs))
    return {"profile_size": size}


def _size(v) -> str:
    return v if v in SIZES else profile_size_pref()


def memory_text(store: Store, target: str, size: Optional[str] = None) -> str:
    """The text ctxport would put into `target` (accepted memories only, secrets masked)."""
    from .distill import out_dir, profile_file
    if target in ("chatgpt", "claude-ai", "gemini-web", "cursor") and (out_dir() / "profile.md").exists():
        from .redact import redact as _redact
        return _redact(profile_file("short" if target == "chatgpt" else _size(size)).read_text(encoding="utf-8"))
    from .adapters import get_exporter
    from .adapters.base import ExportOptions
    if target == "chatgpt":
        from .profile import build_profile
        text, _info = build_profile(store, topics=0, max_chars=WEB_TARGETS["chatgpt"]["limit"] - 50)
        return text
    if target in ("claude-ai", "gemini-web", "cherry-studio", "cursor"):
        files = get_exporter(target).render(store, store.memories(status=("accepted",)), ExportOptions())
        return files[0].content if files else ""
    if target == "profile":
        from .profile import build_profile
        return build_profile(store)[0]
    raise KeyError(target)


# ---------------------------------------------------------------- actions

def _managed_status(path: Path) -> dict:
    from .utils import BLOCK_BEGIN
    exists = path.exists()
    has_block = exists and BLOCK_BEGIN in path.read_text(encoding="utf-8", errors="replace")
    return {"path": str(path), "exists": exists, "imported": bool(has_block)}


def agent_targets() -> list:
    from .adapters import get_exporter
    from .adapters.base import ExportOptions
    out = []
    for key, label in AGENT_TARGETS.items():
        ex = get_exporter(key)
        if key == "cursor":
            out.append({"key": key, "label": label, "path": "项目里的 .cursor/rules/ctxport.mdc，或粘贴到 User Rules",
                        "exists": Path.home().joinpath(".cursor").exists(), "imported": False, "needs_project": True})
            continue
        p = ex.target_path(ExportOptions())
        st = _managed_status(p)
        st.update({"key": key, "label": label, "installed": p.parent.exists()})
        out.append(st)
    return out


def apply_agent(key: str, project: Optional[str] = None, size: Optional[str] = None) -> dict:
    from .adapters.base import ExportOptions
    from .cli import export_and_apply
    if key not in AGENT_TARGETS:
        raise KeyError(key)
    if key == "cursor" and not project:
        raise ValueError("Cursor 需要选一个项目文件夹；全局规则请用“复制”粘贴到 Cursor 设置 → Rules → User Rules")
    proj = Path(project).expanduser() if project else None
    if proj is not None and not proj.is_dir():
        raise ValueError(f"项目文件夹不存在：{proj}")
    with Store() as s:
        from .distill import out_dir
        r = export_and_apply(s, key, ExportOptions(out_dir=exports_dir(), apply=True, project=proj,
                                                   extra={"distilled": (out_dir() / "profile.md").exists(),
                                                          "profile_size": _size(size)}))
    return {"ok": True, "applied": r["applied"]}


def export_app(key: str) -> dict:
    """Write the import file(s) for Chatbox / Cherry Studio, reveal them and open the app."""
    from .adapters.base import ExportOptions
    from .cli import export_and_apply
    if key not in APP_TARGETS:
        raise KeyError(key)
    with Store() as s:
        r = export_and_apply(s, key, ExportOptions(out_dir=exports_dir()))
    files = r["written"]
    if files:
        reveal(files[0])
    open_app(APP_TARGETS[key]["app"])
    return {"ok": True, "files": files, "instructions": r["instructions"]}


def reveal(path: str) -> None:
    p = Path(path)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
    except OSError:
        pass


def open_app(name: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", name], stderr=subprocess.DEVNULL)
    except OSError:
        pass


def overview() -> dict:
    with Store() as s:
        st = s.stats()
        mem = {"accepted": len(s.memories(status=("accepted",))), "candidate": len(s.memories(status=("candidate",)))}
    receiver = {"running": False}
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8765/ping", timeout=1) as r:
            d = json.loads(r.read())
            receiver = {"running": True, "paired": d.get("paired")}
    except Exception:
        pass
    return {"version": __version__, "platforms": st.get("platforms", {}), "memories": mem, "receiver": receiver}


def memories_list(status: str) -> list:
    with Store() as s:
        rows = s.memories(status=(status,))
    return [{"id": m.id, "text": m.text, "category": m.category, "scope": m.scope, "platform": m.platform,
             "status": m.status, "seen": getattr(m, "seen_count", 1) or 1} for m in rows]


def memories_update(payload: dict) -> dict:
    ids = payload.get("ids") or ([payload["id"]] if payload.get("id") else [])
    status = payload.get("status")
    text = payload.get("text")
    n = 0
    with Store() as s:
        for mid in ids:
            if text is not None and len(ids) == 1:
                s.update_memory_text(mid, str(text).strip(), payload.get("category"),
                                     status=status if status in ("accepted", "candidate") else "accepted")
                n += 1
            elif status in ("accepted", "candidate", "rejected"):
                n += s.set_memory_status(mid, status)
        s.commit()
    return {"ok": True, "updated": n}


def targets() -> dict:
    from . import connect
    apps = []
    for key, t in APP_TARGETS.items():
        installed = (sys.platform != "darwin") or any(
            Path(d, t["app"] + ".app").exists() for d in ("/Applications", str(Path.home() / "Applications")))
        apps.append({"key": key, "label": t["label"], "installed": installed})
    return {"agents": agent_targets(), "mcp": connect.all_status(),
            "web": [{"key": k, **v} for k, v in WEB_TARGETS.items()], "apps": apps}


# ---------------------------------------------------------------- distillation (user-chosen model)

_DISTILL = {"running": False, "lines": [], "result": None, "error": None, "stop": False, "progress": None,
            "usage": None, "halted": "", "waiting_until": None, "agent_now": ""}
_DISTILL_LOCK = threading.Lock()


def llm_settings() -> dict:
    from . import llm as L
    cfg = L.load_config()
    if cfg.get("api_key"):
        cfg["api_key"] = "***" + str(cfg["api_key"])[-4:]
    return {"config": cfg, "presets": L.PRESETS}


def save_llm(payload: dict) -> dict:
    from . import llm as L
    cfg = L.load_config()
    preset = payload.get("preset") or cfg.get("preset")
    if preset and preset not in L.PRESETS:
        raise ValueError("unknown preset")
    cfg["preset"] = preset
    cfg["base_url"] = (payload.get("base_url") or "").strip() or (L.PRESETS[preset]["base_url"] if preset else "")
    cfg["model"] = (payload.get("model") or "").strip()
    if payload.get("api_key") and not str(payload["api_key"]).startswith("***"):
        cfg["api_key"] = payload["api_key"].strip()
    cfg["allow_remote"] = bool(payload.get("allow_remote"))
    L.save_config(cfg)
    return llm_settings()


def distill_status() -> dict:
    from .distill import out_dir
    p = out_dir() / "profile.md"
    runs = sorted(out_dir().glob("run-*.json")) if out_dir().exists() else []
    last = json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None
    with _DISTILL_LOCK:
        st = {k: (list(v) if isinstance(v, list) else v) for k, v in _DISTILL.items()}
    st["lines"] = st["lines"][-30:]
    from .distill import profile_file
    sizes = {k: len(profile_file(k).read_text(encoding="utf-8")) for k in SIZES} if p.exists() else {}
    st.update({"profile_exists": p.exists(), "profile_chars": len(p.read_text(encoding="utf-8")) if p.exists() else 0,
               "last_run": last, "sizes": sizes})
    return st


def distill_estimate() -> dict:
    from .distill import estimate
    from .llm import client_from_config
    c = client_from_config()  # raises with a clear message when no model is set or remote isn't allowed
    with Store() as s:
        e = estimate(s)
    e.update({"model": c.model, "base_url": c.base_url})
    return e


def import_downloads() -> dict:
    """Import official ChatGPT / Claude.ai export zips found in the Downloads folder."""
    from .adapters.base import downloads_dir
    from .pipeline import import_path
    out = {}
    with Store() as s:
        for plat in ("chatgpt", "claude-ai"):
            r = import_path(s, plat, downloads_dir())
            out[plat] = {"zips": r.units, "imported": r.imported, "conversations": r.conversations}
    return out


def web_data_all() -> dict:
    """Per site: what the browser extension collected, and whether its sync is on."""
    from . import ingest
    with Store() as s:
        return {"sites": [ingest.web_data(s, p) for p in ingest.PLATFORMS]}


def web_forget(platform: str, keep_web_sync: bool = False) -> dict:
    from . import ingest
    with Store() as s:
        r = ingest.forget_web(s, platform, keep_web_sync=keep_web_sync)
        if r["conversations"]:
            s.compact()
    return r


def distill_options() -> dict:
    from .distill import agent_choices, agent_commands
    found = agent_commands()
    return {"agents": sorted(found), "choices": agent_choices(), "paths": found}


def _limit(v) -> Optional[int]:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


_PREVIEW_CACHE: dict = {}


def distill_preview(limit: Optional[int], agent: str = "", model: str = "", effort: str = "", mode: str = "direct") -> dict:
    """Counting what is left reads every conversation (seconds for thousands), so the answer is kept until the
    conversations or the cards change."""
    from .distill import agent_pending, lean_pending, measured_per_conversation, open_cache
    mode = "agent" if mode == "agent" else "direct"
    with Store() as s:
        total, newest = s.db.execute("SELECT COUNT(*), MAX(updated_at) FROM conversations").fetchone()
        cache = open_cache()
        cards, card_t = cache.execute("SELECT COUNT(*), MAX(created_at) FROM cards").fetchone()
        cache.close()
        key = (mode, limit, total, newest, cards, card_t)
        r = dict(_PREVIEW_CACHE[key]) if key in _PREVIEW_CACHE else None
        if r is None:
            r = agent_pending(s, limit=limit) if mode == "agent" else lean_pending(s, limit=limit)
            if len(_PREVIEW_CACHE) > 20:
                _PREVIEW_CACHE.clear()
            _PREVIEW_CACHE[key] = dict(r)
    r["total"] = total
    per = measured_per_conversation(agent, model, effort, mode) if agent else None
    if per:
        n = r["conversations"]
        r["measured"] = {**per, "projected": {k: round(per[k] * n) for k in ("input", "cache_read", "cache_write", "output")}}
    return r


def distill_profile(size: str = "full") -> dict:
    from .distill import out_dir, profile_file
    if not (out_dir() / "profile.md").exists():
        return {"text": "", "path": str(out_dir() / "profile.md")}
    p = profile_file(size if size in SIZES else "full")
    return {"text": p.read_text(encoding="utf-8"), "path": str(p)}


def distill_stop() -> dict:
    with _DISTILL_LOCK:
        if _DISTILL["running"]:
            _DISTILL["stop"] = True
    return {"ok": True}


def _distill_thread(job) -> None:
    def note(line: str) -> None:
        with _DISTILL_LOCK:
            _DISTILL["lines"].append(line)

    def work() -> None:
        try:
            res = job(note)
            with _DISTILL_LOCK:
                _DISTILL["result"] = res
        except Exception as e:
            with _DISTILL_LOCK:
                _DISTILL["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _DISTILL_LOCK:
                _DISTILL["running"] = False

    with _DISTILL_LOCK:
        if _DISTILL["running"]:
            raise ValueError("已经在提炼中")
        _DISTILL.update(running=True, lines=[], result=None, error=None, stop=False, progress=None, usage=None, halted="",
                        waiting_until=None, agent_now="")
    threading.Thread(target=work, daemon=True).start()


def build_profile_now(note=None) -> dict:
    """Import whatever cards the agent folder / web replies hold, then merge and write (no model needed)."""
    from . import distill as D
    with Store() as s:
        if (D.work_dir("agent") / "cards").is_dir():
            D.agent_import(s, D.work_dir("agent"))
        if (D.work_dir("web") / "responses").is_dir():
            D.manual_import(s, D.work_dir("web"))
        r = D.run(s, None, polish=False, progress=note)
    return {"chars": r["chars"], "log": r["log"]}


def _keep_awake():
    """macOS: stop idle sleep while a long run or a wait for the quota is going on (released when it ends)."""
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        return None
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except OSError:
        return None


def distill_agent(agent: str, model: str = "", effort: str = "", limit: Optional[int] = None, parallel: int = 1,
                  fallback: Optional[dict] = None, wait: bool = False, mode: str = "direct") -> dict:
    """Run the agent; when its quota runs out, optionally continue with `fallback` ({agent, model, effort}) and/or
    wait until the quota is back (the time the agent named, else 30 minutes) and continue."""
    from . import distill as D
    found = D.agent_commands()
    if agent not in found:
        raise ValueError(f"没有找到 {agent} 命令行")
    D.agent_command(agent, "x", "", model, effort)   # reject a bad model / effort before starting
    plan = [(agent, model, effort)]
    if fallback and fallback.get("agent") and fallback["agent"] != agent:
        fb = (str(fallback["agent"]), str(fallback.get("model") or ""), str(fallback.get("effort") or ""))
        if fb[0] not in found:
            raise ValueError(f"没有找到 {fb[0]} 命令行，不能用它接着跑")
        D.agent_command(fb[0], "x", "", fb[1], fb[2])
        plan.append(fb)

    def stopping() -> bool:
        with _DISTILL_LOCK:
            return _DISTILL["stop"]

    def batch(i: int, n: int) -> None:
        with _DISTILL_LOCK:
            _DISTILL["progress"] = {"done": i, "total": n}

    def usage(u: dict) -> None:
        with _DISTILL_LOCK:
            _DISTILL["usage"] = u

    def one_pass(s, a, m, e, note) -> Optional[dict]:
        label = " · ".join(x for x in (a, m or "默认模型", e and f"推理 {e}", parallel > 1 and f"{parallel} 批并行") if x)
        with _DISTILL_LOCK:
            _DISTILL.update(agent_now=a, halted="", waiting_until=None)
        if mode == "agent":
            prep = D.agent_prepare(s, D.work_dir("agent"), limit=limit)
            note(f"{label}：{prep['conversations']} 个对话需要情节卡，共 {len(prep['batches'])} 批")
            if not prep["conversations"]:
                return None
            r = D.run_agent(s, D.work_dir("agent"), a, progress=note, model=m, effort=e, should_stop=stopping,
                            on_batch=batch, on_usage=usage, parallel=parallel)
        else:
            if D.work_dir("agent").joinpath("cards").is_dir():
                D.agent_import(s, D.work_dir("agent"))      # cards an earlier agent run left behind
            pend = D.lean_pending(s, limit=limit)
            note(f"{label} · 省 token：{pend['conversations']} 个对话需要情节卡，分 {pend['batches']} 组提问")
            if not pend["conversations"]:
                return None
            r = D.run_direct(s, a, model=m, effort=e, limit=limit, parallel=parallel, progress=note,
                             should_stop=stopping, on_batch=batch, on_usage=usage)
        note(f"完成 {r['batches_run']} 批，失败 {r['batches_failed']} 批，导入 {r['imported']} 张卡"
             + ("；已按你的要求停止，再点开始会接着做" if r["stopped"] else ""))
        return r

    def job(note):
        awake = _keep_awake()
        try:
            with Store() as s:
                while True:
                    reasons = []
                    for a, m, e in plan:
                        r = one_pass(s, a, m, e, note)
                        if not r or not r.get("halted"):
                            return build_profile_now(note)       # finished, nothing left, or stopped by the user
                        reasons.append(r["halted"])
                        with _DISTILL_LOCK:
                            _DISTILL["halted"] = r["halted"]
                        if (a, m, e) != plan[-1]:
                            note(f"{a} 额度用完了，换成 {plan[plan.index((a, m, e)) + 1][0]} 接着跑")
                    if not wait or stopping():
                        return build_profile_now(note)
                    build_profile_now(note)                       # the profile is usable while we wait
                    times = [t for t in (D.limit_reset_time(x) for x in reasons) if t]
                    until = (min(times) if times else time.time() + 1800) + 60
                    with _DISTILL_LOCK:
                        _DISTILL["waiting_until"] = until
                    note(f"额度都用完了，等到 {time.strftime('%H:%M', time.localtime(until))} 自动继续"
                         "（电脑会保持不睡眠；点“停止”可以取消）")
                    while time.time() < until:
                        if stopping():
                            return build_profile_now(note)
                        time.sleep(2)
        finally:
            with _DISTILL_LOCK:
                _DISTILL["waiting_until"] = None
            if awake:
                awake.terminate()

    _distill_thread(job)
    return {"ok": True}


def distill_folder() -> dict:
    from . import distill as D
    with Store() as s:
        prep = D.agent_prepare(s, D.work_dir("agent"))
    reveal(str(Path(prep["folder"]) / "TASK.md"))
    return prep


def distill_build() -> dict:
    _distill_thread(build_profile_now)
    return {"ok": True}


def web_list(limit: Optional[int] = None) -> dict:
    from . import distill as D
    with Store() as s:
        return {"batches": D.web_batches(s, D.work_dir("web"), limit=limit)}


def web_prompt(name: str) -> str:
    import re as _re
    from . import distill as D
    if not _re.fullmatch(r"\d{4}\.md", name or ""):
        raise ValueError("bad batch name")
    return (D.work_dir("web") / "prompts" / name).read_text(encoding="utf-8")


def web_answer(name: str, reply: str) -> dict:
    from . import distill as D
    with Store() as s:
        n = D.web_answer(s, D.work_dir("web"), name, reply)
    return {"ok": True, "cards": n}


def distill_start() -> dict:
    from . import distill as D
    from .llm import client_from_config
    client = client_from_config()
    with _DISTILL_LOCK:
        if _DISTILL["running"]:
            raise ValueError("已经在提炼中")
        _DISTILL.update(running=True, lines=[], result=None, error=None, stop=False, progress=None)

    def note(line: str) -> None:
        with _DISTILL_LOCK:
            _DISTILL["lines"].append(line)

    def work() -> None:
        try:
            with Store() as s:
                r = D.run(s, client, progress=note)
            with _DISTILL_LOCK:
                _DISTILL["result"] = {"chars": r["chars"], "log": r["log"]}
        except Exception as e:
            with _DISTILL_LOCK:
                _DISTILL["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _DISTILL_LOCK:
                _DISTILL["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------- HTTP

class AppHandler(BaseHTTPRequestHandler):
    server_version = f"ctxport-app/{__version__}"
    token = ""
    port = 0
    quiet = True

    def log_message(self, fmt, *args):  # noqa: N802
        if not self.quiet:
            super().log_message(fmt, *args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _api_allowed(self) -> bool:
        if not (self._host_ok() and self._origin_ok()):
            self._json(403, {"error": "forbidden"})
            return False
        if not secrets.compare_digest(self.headers.get("X-Ctxport-App") or "", self.token):
            self._json(403, {"error": "invalid app token; reopen the page with `ctxport app`"})
            return False
        return True

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            if not self._host_ok():
                return self._json(403, {"error": "forbidden"})
            page = resources.files("ctxport").joinpath("web/index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")
        if not u.path.startswith("/api/") or not self._api_allowed():
            return None if u.path.startswith("/api/") else self._json(404, {"error": "not found"})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/overview":
                return self._json(200, overview())
            if u.path == "/api/memories":
                return self._json(200, {"memories": memories_list(q.get("status", "accepted"))})
            if u.path == "/api/targets":
                return self._json(200, targets())
            if u.path == "/api/llm":
                return self._json(200, llm_settings())
            if u.path == "/api/web-data":
                return self._json(200, web_data_all())
            if u.path == "/api/distill/status":
                return self._json(200, distill_status())
            if u.path == "/api/distill/options":
                return self._json(200, distill_options())
            if u.path == "/api/prefs":
                return self._json(200, {"profile_size": profile_size_pref()})
            if u.path == "/api/distill/preview":
                return self._json(200, distill_preview(_limit(q.get("limit")), q.get("agent", ""), q.get("model", ""),
                                                       q.get("effort", ""), q.get("mode", "direct")))
            if u.path == "/api/distill/profile":
                return self._json(200, distill_profile(q.get("size", "full")))
            if u.path == "/api/distill/web":
                lim = q.get("limit", "")
                return self._json(200, web_list(int(lim) if lim.isdigit() and int(lim) > 0 else None))
            if u.path == "/api/distill/web/prompt":
                return self._json(200, {"text": web_prompt(q.get("name", ""))})
            if u.path == "/api/text":
                with Store() as s:
                    return self._json(200, {"text": memory_text(s, q.get("target", ""), q.get("size"))})
        except KeyError as e:
            return self._json(400, {"error": f"unknown target {e}"})
        except (ValueError, RuntimeError, FileNotFoundError) as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._api_allowed():
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return self._json(413, {"error": "bad length"})
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "expected an object"})
        from . import connect
        path = urlparse(self.path).path
        try:
            if path == "/api/llm":
                return self._json(200, save_llm(payload))
            if path == "/api/distill/agent":
                if payload.get("confirm") is not True:
                    return self._json(400, {"error": "confirmation required"})
                return self._json(200, distill_agent(str(payload.get("agent")), str(payload.get("model") or ""),
                                                     str(payload.get("effort") or ""), _limit(payload.get("limit")),
                                                     min(_limit(payload.get("parallel")) or 1, 16),
                                                     payload.get("fallback") if isinstance(payload.get("fallback"), dict) else None,
                                                     bool(payload.get("wait")),
                                                     "agent" if payload.get("mode") == "agent" else "direct"))
            if path == "/api/distill/stop":
                return self._json(200, distill_stop())
            if path == "/api/distill/reveal":
                prof = distill_profile()
                if not prof["text"]:
                    raise ValueError("还没有档案，先完成第 2 步")
                reveal(prof["path"])
                return self._json(200, {"ok": True})
            if path == "/api/web-data/forget":
                if payload.get("confirm") is not True:
                    return self._json(400, {"error": "confirmation required"})
                return self._json(200, web_forget(str(payload.get("platform")), bool(payload.get("keep_web_sync"))))
            if path == "/api/web-data/sync":
                from . import ingest
                ingest.set_web_sync(str(payload.get("platform")), bool(payload.get("on")))
                return self._json(200, web_data_all())
            if path == "/api/import-downloads":
                return self._json(200, import_downloads())
            if path == "/api/distill/folder":
                return self._json(200, distill_folder())
            if path == "/api/distill/build":
                return self._json(200, distill_build())
            if path == "/api/distill/web/answer":
                return self._json(200, web_answer(str(payload.get("name")), str(payload.get("reply") or "")))
            if path == "/api/distill/estimate":
                return self._json(200, distill_estimate())
            if path == "/api/distill/start":
                if payload.get("confirm") is not True:
                    return self._json(400, {"error": "confirmation required"})
                return self._json(200, distill_start())
            if path == "/api/memories/update":
                return self._json(200, memories_update(payload))
            if path == "/api/prefs":
                return self._json(200, set_profile_size_pref(str(payload.get("profile_size"))))
            if path == "/api/agent/apply":
                return self._json(200, apply_agent(str(payload.get("key")), payload.get("project") or None,
                                                   _size(payload.get("size"))))
            if path == "/api/mcp/connect":
                key = str(payload.get("key"))
                if key not in connect.TARGETS:
                    raise KeyError(key)
                return self._json(200, connect.connect(key))
            if path == "/api/mcp/disconnect":
                key = str(payload.get("key"))
                if key not in connect.TARGETS:
                    raise KeyError(key)
                return self._json(200, connect.disconnect(key))
            if path == "/api/app/export":
                return self._json(200, export_app(str(payload.get("key"))))
            if path == "/api/open-url":
                url = str(payload.get("url") or "")
                allowed = {v["url"] for v in WEB_TARGETS.values()}
                if url not in allowed:
                    return self._json(400, {"error": "url not allowed"})
                webbrowser.open(url)
                return self._json(200, {"ok": True})
        except KeyError as e:
            return self._json(400, {"error": f"unknown target {e}"})
        except (ValueError, RuntimeError) as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        return self._json(404, {"error": "not found"})


def make_server(port: int = 8766, token: Optional[str] = None) -> ThreadingHTTPServer:
    from .utils import local_http_server
    srv = local_http_server(("127.0.0.1", port), AppHandler)
    AppHandler.port = srv.server_address[1]
    AppHandler.token = token or secrets.token_urlsafe(24)
    return srv


def _url_file() -> Path:
    return default_home() / "app-url"


def running_url(port: int) -> Optional[str]:
    """URL (with token) of a ctxport app already serving on `port`, if there is one."""
    try:
        url = _url_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if urlparse(url).port != port:
        return None
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return None
    return url


def serve(port: int = 8766, open_browser: bool = True) -> None:
    try:
        srv = make_server(port)
    except OSError:
        url = running_url(port)
        if not url:
            raise
        print(f"ctxport app is already running: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    url = f"http://127.0.0.1:{AppHandler.port}/#t={AppHandler.token}"
    ensure_private_dir(default_home())
    private_write_text(_url_file(), url)   # lets a second launch (the desktop launcher) reopen this one
    print(f"ctxport app: {url}\n(only reachable from this computer; Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            if _url_file().read_text(encoding="utf-8").strip() == url:
                _url_file().unlink()
        except OSError:
            pass


LAUNCHER_MARK = "ctxport-launcher"


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def make_launcher(folder: Path) -> Path:
    """A double-clickable ctxport.app (macOS) that starts the app in the background, or reopens the running one.

    The PATH of this shell is baked in: apps started from Finder get a bare PATH, and the app needs it to find
    `claude` and `codex` for distillation."""
    if sys.platform != "darwin" or not shutil.which("osacompile"):
        raise RuntimeError("the launcher is only available on macOS")
    exe = shutil.which("ctxport") or str(Path(sys.argv[0]).resolve())
    dest = Path(folder).expanduser() / "ctxport.app"
    if dest.exists():
        if not (dest / "Contents" / "Resources" / LAUNCHER_MARK).exists():
            raise RuntimeError(f"{dest} exists and is not a ctxport launcher; not replacing it")
        shutil.rmtree(dest)
    log = default_home() / "app.log"
    path = os.environ.get("PATH", "")
    cmd = ("export PATH=" + shlex.quote(path) + "; nohup " + shlex.quote(exe) + " app >> " + shlex.quote(str(log))
           + " 2>&1 &")
    script = "do shell script " + _applescript_str(cmd)
    subprocess.run(["osacompile", "-o", str(dest), "-e", script], check=True, capture_output=True, text=True)
    (dest / "Contents" / "Resources" / LAUNCHER_MARK).write_text(exe + "\n", encoding="utf-8")
    return dest

