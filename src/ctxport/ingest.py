"""Local ingest server for the ctxport browser extension.

    ctxport serve-ingest            # http://127.0.0.1:8765, stdlib http.server

* Listens on 127.0.0.1 only.
* Accepts requests only from a browser-extension origin (chrome-extension:// / extension://), so ordinary
  web pages cannot write into your store. The first extension that connects is paired (its origin is saved
  in ~/.ctxport/ingest-origin); others are refused until `ctxport serve-ingest --reset-pairing`.
* Each received conversation is saved as raw JSON under ~/.ctxport/inbox/<platform>/<id>.json and imported
  immediately with the normal importer (so `ctxport sync` can also re-import the inbox later).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import __version__
from .store import Store, default_home

PLATFORMS = ("chatgpt", "claude-ai", "gemini-web")
MAX_BODY = 200 * 1024 * 1024
_EXT_ORIGIN = re.compile(r"^(chrome-extension|extension|moz-extension|safari-web-extension)://[a-z0-9.-]+$", re.I)
_LOCK = threading.Lock()


def inbox_dir(platform: Optional[str] = None) -> Path:
    d = default_home() / "inbox"
    return d / platform if platform else d


def _origin_file() -> Path:
    return default_home() / "ingest-origin"


def _safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(s))[:120]
    return s or hashlib.sha1(str(s).encode()).hexdigest()[:16]


def conversation_key(platform: str, conv: dict) -> str:
    if platform == "chatgpt":
        return str(conv.get("conversation_id") or conv.get("id") or "")
    if platform == "claude-ai":
        return str(conv.get("uuid") or "")
    return str(conv.get("id") or "")


def conversation_stamp(platform: str, conv: dict) -> str:
    return str(conv.get("_ctxport_stamp") or conv.get("update_time") or conv.get("updated_at")
               or conv.get("updatedAt") or len(conv.get("messages") or []))


class IngestState:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path

    def known(self, platform: str) -> dict:
        """{native_id: update stamp} of what is already in the inbox (lets the extension skip unchanged)."""
        out = {}
        idx = inbox_dir(platform) / "_index.json"
        if idx.exists():
            try:
                out = json.loads(idx.read_text(encoding="utf-8"))
            except ValueError:
                out = {}
        return out

    def save(self, platform: str, payload: dict) -> dict:
        from .pipeline import SyncReport, import_unit
        from .adapters import get_importer
        if platform not in PLATFORMS:
            raise ValueError(f"unknown platform {platform!r}")
        d = inbox_dir(platform)
        d.mkdir(parents=True, exist_ok=True)
        convs = payload.get("conversations") or []
        written = []
        with _LOCK:
            index = self.known(platform)
            for c in convs:
                if not isinstance(c, dict):
                    continue
                cid = conversation_key(platform, c)
                if not cid:
                    continue
                p = d / f"{_safe_name(cid)}.json"
                p.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
                index[cid] = conversation_stamp(platform, c)
                written.append(p)
            extras = {k: v for k, v in payload.items() if k in ("memories", "custom_instructions", "projects") and v}
            if extras:
                p = d / "_account.json"
                old = {}
                if p.exists():
                    try:
                        old = json.loads(p.read_text(encoding="utf-8"))
                    except ValueError:
                        old = {}
                old.update(extras)
                p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
                written.append(p)
            (d / "_index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
            imp = get_importer(platform)
            rep = SyncReport(platform)
            with Store(self.db_path) as store:
                for p in written:
                    import_unit(store, imp, p, False, rep)
        return {"saved": len(written), "conversations": rep.conversations, "messages": rep.messages,
                "memories_new": rep.memories_new, "warnings": rep.warnings[:20]}


class Handler(BaseHTTPRequestHandler):
    server_version = f"ctxport-ingest/{__version__}"
    state: IngestState = IngestState()
    quiet = False

    def log_message(self, fmt, *args):  # noqa: N802
        if not self.quiet:
            super().log_message(fmt, *args)

    # --- origin pairing
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin") or ""
        if not _EXT_ORIGIN.match(origin):
            return False
        f = _origin_file()
        with _LOCK:
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(origin, encoding="utf-8")
                return True
            return f.read_text(encoding="utf-8").strip() == origin

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin") or ""
        if _EXT_ORIGIN.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        origin = self.headers.get("Origin") or ""
        if _EXT_ORIGIN.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path == "/ping":
            return self._send(200, {"ok": True, "app": "ctxport", "version": __version__,
                                    "paired": _origin_file().exists()})
        if not self._origin_ok():
            return self._send(403, {"error": "origin not allowed (only the paired ctxport extension)"})
        m = re.match(r"^/known\?platform=([\w-]+)$", self.path)
        if m:
            return self._send(200, {"known": self.state.known(m.group(1))})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/ingest":
            return self._send(404, {"error": "not found"})
        if not self._origin_ok():
            return self._send(403, {"error": "origin not allowed (only the paired ctxport extension)"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._send(413, {"error": "body too large or empty"})
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            result = self.state.save(str(payload.get("platform")), payload)
        except Exception as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        return self._send(200, {"ok": True, **result})


def make_server(port: int = 8765, db_path: Optional[Path] = None, quiet: bool = False) -> ThreadingHTTPServer:
    Handler.state = IngestState(db_path)
    Handler.quiet = quiet
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int = 8765, db_path: Optional[Path] = None) -> None:
    srv = make_server(port, db_path)
    print(f"ctxport ingest server on http://127.0.0.1:{port}  (store: {db_path or default_home() / 'ctxport.db'})")
    print("Waiting for the ctxport browser extension. Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def reset_pairing() -> bool:
    f = _origin_file()
    if f.exists():
        f.unlink()
        return True
    return False


# ---------------------------------------------------------------- autostart (opt-in, user-run command)

def autostart_install(port: int = 8765) -> str:
    import shutil
    import sys
    exe = shutil.which("ctxport")
    cmd = [exe, "serve-ingest", "--port", str(port)] if exe else [sys.executable, "-m", "ctxport", "serve-ingest",
                                                                  "--port", str(port)]
    home = Path.home()
    if sys.platform == "darwin":
        p = home / "Library/LaunchAgents/io.github.ctxport.ingest.plist"
        args = "".join(f"<string>{a}</string>" for a in cmd)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>io.github.ctxport.ingest</string>
<key>ProgramArguments</key><array>{args}</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>{default_home() / 'ingest.log'}</string>
</dict></plist>
""", encoding="utf-8")
        os.system(f"launchctl unload '{p}' 2>/dev/null; launchctl load '{p}'")
        return f"installed {p} (loaded now and at every login)"
    if sys.platform.startswith("linux"):
        p = home / ".config/systemd/user/ctxport-ingest.service"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[Unit]\nDescription=ctxport ingest server\n\n[Service]\nExecStart={' '.join(cmd)}\nRestart=on-failure\n\n"
                     "[Install]\nWantedBy=default.target\n", encoding="utf-8")
        os.system("systemctl --user daemon-reload && systemctl --user enable --now ctxport-ingest.service")
        return f"installed {p} (systemd --user)"
    if sys.platform == "win32":
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
        p = startup / "ctxport-ingest.cmd"
        p.write_text("@echo off\nstart \"\" /min " + " ".join(f'"{c}"' for c in cmd) + "\n", encoding="utf-8")
        return f"installed {p} (starts at login; run it once now or log in again)"
    return "autostart not supported on this platform; run `ctxport serve-ingest` manually"


def autostart_remove() -> str:
    import sys
    home = Path.home()
    if sys.platform == "darwin":
        p = home / "Library/LaunchAgents/io.github.ctxport.ingest.plist"
        if p.exists():
            os.system(f"launchctl unload '{p}' 2>/dev/null")
            p.unlink()
            return f"removed {p}"
    elif sys.platform.startswith("linux"):
        p = home / ".config/systemd/user/ctxport-ingest.service"
        if p.exists():
            os.system("systemctl --user disable --now ctxport-ingest.service")
            p.unlink()
            return f"removed {p}"
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/ctxport-ingest.cmd"
        if p.exists():
            p.unlink()
            return f"removed {p}"
    return "nothing to remove"


def wait_until_up(port: int, timeout: float = 5.0) -> bool:
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
                return r.status == 200
        except Exception:
            time.sleep(0.1)
    return False
