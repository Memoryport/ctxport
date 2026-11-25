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
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .store import Store, default_home

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

def memory_text(store: Store, target: str) -> str:
    """The text ctxport would put into `target` (accepted memories only, secrets masked)."""
    from .distill import out_dir
    if target in ("chatgpt", "claude-ai", "gemini-web", "cursor") and (out_dir() / "profile.md").exists():
        from .redact import redact as _redact
        name = "profile.short.md" if target == "chatgpt" else "profile.md"
        return _redact((out_dir() / name).read_text(encoding="utf-8"))
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


def apply_agent(key: str, project: Optional[str] = None) -> dict:
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
                                                   extra={"distilled": (out_dir() / "profile.md").exists()}))
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

_DISTILL = {"running": False, "lines": [], "result": None, "error": None}
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
    st.update({"profile_exists": p.exists(), "profile_chars": len(p.read_text(encoding="utf-8")) if p.exists() else 0,
               "last_run": last})
    return st


def distill_estimate() -> dict:
    from .distill import estimate
    from .llm import client_from_config
    c = client_from_config()  # raises with a clear message when no model is set or remote isn't allowed
    with Store() as s:
        e = estimate(s)
    e.update({"model": c.model, "base_url": c.base_url})
    return e


def distill_start() -> dict:
    from . import distill as D
    from .llm import client_from_config
    client = client_from_config()
    with _DISTILL_LOCK:
        if _DISTILL["running"]:
            raise ValueError("已经在提炼中")
        _DISTILL.update(running=True, lines=[], result=None, error=None)

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
            if u.path == "/api/distill/status":
                return self._json(200, distill_status())
            if u.path == "/api/text":
                with Store() as s:
                    return self._json(200, {"text": memory_text(s, q.get("target", ""))})
        except KeyError as e:
            return self._json(400, {"error": f"unknown target {e}"})
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
            if path == "/api/distill/estimate":
                return self._json(200, distill_estimate())
            if path == "/api/distill/start":
                if payload.get("confirm") is not True:
                    return self._json(400, {"error": "confirmation required"})
                return self._json(200, distill_start())
            if path == "/api/memories/update":
                return self._json(200, memories_update(payload))
            if path == "/api/agent/apply":
                return self._json(200, apply_agent(str(payload.get("key")), payload.get("project") or None))
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
    srv = ThreadingHTTPServer(("127.0.0.1", port), AppHandler)
    AppHandler.port = srv.server_address[1]
    AppHandler.token = token or secrets.token_urlsafe(24)
    return srv


def serve(port: int = 8766, open_browser: bool = True) -> None:
    srv = make_server(port)
    url = f"http://127.0.0.1:{AppHandler.port}/#t={AppHandler.token}"
    print(f"ctxport app: {url}\n(only reachable from this computer; Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()

