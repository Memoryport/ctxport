"""Local ingest server for the ctxport browser extension.

    ctxport serve-ingest            # http://127.0.0.1:8765, stdlib http.server

* Listens on 127.0.0.1 only.
* Accepts requests only from a browser-extension origin (chrome-extension:// / extension://), so ordinary
  web pages cannot write into your store.
* Pairing needs a one-time code: `serve-ingest` prints it when nothing is paired yet, and
  `ctxport serve-ingest --pair` prints a fresh one at any time. The code is only readable by your OS account
  (~/.ctxport is 0700), expires after 10 minutes and allows 5 attempts. Typing it into the extension popup
  gives that extension a random token; every later request must carry both the paired origin and the token
  (only its SHA-256 is stored, in ~/.ctxport/ingest-pairing.json). Another extension, or a local process that
  forges an Origin header, cannot connect without the code.
* Each received conversation is saved as raw JSON under ~/.ctxport/inbox/<platform>/<id>.json (0600) and
  imported immediately with the normal importer (so `ctxport sync` can also re-import the inbox later).
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import __version__
from .store import Store, default_home, ensure_private_dir, private_write_bytes, private_write_text

PLATFORMS = ("chatgpt", "claude-ai", "gemini-web")
MAX_BODY = 200 * 1024 * 1024
_EXT_ORIGIN = re.compile(r"^(chrome-extension|extension|moz-extension|safari-web-extension)://[a-z0-9.-]+$", re.I)
_LOCK = threading.Lock()
TOKEN_HEADER = "X-Ctxport-Token"
PAIR_CODE_TTL = 600          # seconds
PAIR_CODE_ATTEMPTS = 5
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


def inbox_dir(platform: Optional[str] = None) -> Path:
    d = default_home() / "inbox"
    return d / platform if platform else d


def _pairing_file() -> Path:
    return default_home() / "ingest-pairing.json"


def _code_file() -> Path:
    return default_home() / "ingest-pairing-code.json"


def _legacy_origin_file() -> Path:  # pre-token pairing (origin only); no longer trusted
    return default_home() / "ingest-origin"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _read_json(p: Path) -> Optional[dict]:
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def pairing() -> Optional[dict]:
    """{origin, token_sha256, paired_at} of the paired extension, or None."""
    p = _read_json(_pairing_file())
    if p and isinstance(p.get("origin"), str) and isinstance(p.get("token_sha256"), str):
        return p
    return None


def is_paired() -> bool:
    return pairing() is not None


def new_pairing_code() -> str:
    """Create a fresh one-time pairing code (replacing any previous one) and return it for display."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    ensure_private_dir(default_home())
    with _LOCK:
        private_write_text(_code_file(), json.dumps({"code_sha256": _sha256(raw), "expires": time.time() + PAIR_CODE_TTL,
                                                     "attempts_left": PAIR_CODE_ATTEMPTS}))
    return f"{raw[:4]}-{raw[4:]}"


def _normalize_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()


def try_pair(origin: str, code: str) -> Optional[str]:
    """Check a pairing code; on success pair `origin` and return its new token (shown to the extension once)."""
    if not _EXT_ORIGIN.match(origin or ""):
        return None
    with _LOCK:
        f = _code_file()
        st = _read_json(f)
        if not st:
            return None
        if time.time() > float(st.get("expires") or 0) or int(st.get("attempts_left") or 0) <= 0:
            f.unlink(missing_ok=True)
            return None
        if not hmac.compare_digest(_sha256(_normalize_code(code)), str(st.get("code_sha256") or "")):
            st["attempts_left"] = int(st.get("attempts_left") or 0) - 1
            if st["attempts_left"] <= 0:
                f.unlink(missing_ok=True)
            else:
                private_write_text(f, json.dumps(st))
            return None
        f.unlink(missing_ok=True)  # one-time
        token = secrets.token_urlsafe(32)
        private_write_text(_pairing_file(), json.dumps({"origin": origin, "token_sha256": _sha256(token),
                                                        "paired_at": time.time()}))
        _legacy_origin_file().unlink(missing_ok=True)
        return token


def authorized(origin: str, token: str) -> bool:
    """The token is the credential; the Origin is an extra check whenever the browser sends one.

    Chromium does not attach an Origin header to GET requests an extension makes to a host it has
    permission for, so a paired extension's /ping and /known arrive without one. Web pages always send
    an Origin on cross-origin requests, so a present Origin must be the paired extension's.
    """
    if not token:
        return False
    if origin and not _EXT_ORIGIN.match(origin):
        return False
    p = pairing()
    if not p:
        return False
    if origin and not hmac.compare_digest(p["origin"], origin):
        return False
    return hmac.compare_digest(p["token_sha256"], _sha256(token))


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
        d = ensure_private_dir(inbox_dir(platform))
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
                # Raw page JSON is verbose (ChatGPT: ~0.6 MB per conversation, mostly metadata); gzip keeps
                # it re-parseable at about 1/6 of the size.
                p = d / f"{_safe_name(cid)}.json.gz"
                private_write_bytes(p, gzip.compress(json.dumps(c, ensure_ascii=False).encode("utf-8"), 6))
                stale = d / f"{_safe_name(cid)}.json"
                if stale.exists():
                    stale.unlink()
                index[cid] = conversation_stamp(platform, c)
                written.append(p)
            extras = {k: v for k, v in payload.items() if k in ("memories", "custom_instructions", "projects") and v}
            if extras:
                # One file per account, so a second Claude.ai organisation doesn't overwrite the first one's
                # projects and memories.
                account = payload.get("account") if isinstance(payload.get("account"), str) else ""
                p = d / (f"_account.{_safe_name(account)}.json" if account else "_account.json")
                old = {}
                if p.exists():
                    try:
                        old = json.loads(p.read_text(encoding="utf-8"))
                    except ValueError:
                        old = {}
                old.update(extras)
                private_write_text(p, json.dumps(old, ensure_ascii=False))
                written.append(p)
            private_write_text(d / "_index.json", json.dumps(index, ensure_ascii=False))
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

    # --- pairing / auth
    def _origin(self) -> str:
        return self.headers.get("Origin") or ""

    def _authorized(self) -> bool:
        return authorized(self._origin(), self.headers.get(TOKEN_HEADER) or "")

    def _deny(self):
        if not _EXT_ORIGIN.match(self._origin()):
            return self._send(403, {"error": "origin not allowed (only the paired ctxport extension)"})
        return self._send(403, {"error": "not paired: run `ctxport serve-ingest --pair` and enter the code in the "
                                         "extension popup", "need_pairing": True})

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0 or n > MAX_BODY:
            self._send(413, {"error": "body too large, empty or without a valid Content-Length"})
            return None
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._send(400, {"error": f"invalid JSON: {e}"})
            return None
        if not isinstance(payload, dict):
            self._send(400, {"error": "expected a JSON object"})
            return None
        return payload

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
            self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path == "/ping":
            return self._send(200, {"ok": True, "app": "ctxport", "version": __version__,
                                    "paired": is_paired(), "authorized": self._authorized()})
        if not self._authorized():
            return self._deny()
        m = re.match(r"^/known\?platform=([\w-]+)$", self.path)
        if m:
            return self._send(200, {"known": self.state.known(m.group(1))})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path == "/pair":
            if not _EXT_ORIGIN.match(self._origin()):
                return self._deny()
            payload = self._read_json_body()
            if payload is None:
                return None
            token = try_pair(self._origin(), str(payload.get("code") or ""))
            if not token:
                return self._send(403, {"error": "wrong or expired pairing code; get a new one with "
                                                 "`ctxport serve-ingest --pair`", "need_pairing": True})
            return self._send(200, {"ok": True, "token": token})
        if self.path != "/ingest":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._deny()
        payload = self._read_json_body()
        if payload is None:
            return None
        try:
            result = self.state.save(str(payload.get("platform")), payload)
        except Exception as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        return self._send(200, {"ok": True, **result})


def compress_inbox() -> tuple:
    """gzip any plain `<id>.json` conversation files in the inbox (older versions wrote them uncompressed).

    Returns (files, bytes_before, bytes_after)."""
    n = before = after = 0
    root = inbox_dir()
    if not root.is_dir():
        return 0, 0, 0
    for p in root.glob("*/*.json"):
        if p.name.startswith("_"):
            continue
        raw = p.read_bytes()
        gz = p.with_name(p.name + ".gz")
        private_write_bytes(gz, gzip.compress(raw, 6))
        before += len(raw)
        after += gz.stat().st_size
        p.unlink()
        n += 1
    return n, before, after


def make_server(port: int = 8765, db_path: Optional[Path] = None, quiet: bool = False) -> ThreadingHTTPServer:
    Handler.state = IngestState(db_path)
    Handler.quiet = quiet
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def print_pairing_code() -> str:
    code = new_pairing_code()
    print(f"Pairing code: {code}   (enter it in the ctxport extension popup; valid 10 minutes, one use)")
    return code


def serve(port: int = 8765, db_path: Optional[Path] = None) -> None:
    srv = make_server(port, db_path)
    print(f"ctxport ingest server on http://127.0.0.1:{port}  (store: {db_path or default_home() / 'ctxport.db'})")
    if not is_paired():
        if _legacy_origin_file().exists():
            print("This version pairs with a one-time code; please pair the extension again.")
        print_pairing_code()
    print("Waiting for the ctxport browser extension. Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def reset_pairing() -> bool:
    found = False
    for f in (_pairing_file(), _code_file(), _legacy_origin_file()):
        if f.exists():
            f.unlink()
            found = True
    return found


# ---------------------------------------------------------------- autostart (opt-in, user-run command)

def _run(argv: list) -> int:
    """Run a service-manager command without a shell (paths with spaces or quotes are passed verbatim)."""
    try:
        return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode
    except OSError:
        return 127


def _systemd_quote(arg: str) -> str:
    """Quote one ExecStart= argument for systemd (double quotes; backslash, quote and % escaped)."""
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def autostart_install(port: int = 8765) -> str:
    import shutil
    import sys
    exe = shutil.which("ctxport")
    cmd = [exe, "serve-ingest", "--port", str(port)] if exe else [sys.executable, "-m", "ctxport", "serve-ingest",
                                                                  "--port", str(port)]
    home = Path.home()
    if sys.platform == "darwin":
        from xml.sax.saxutils import escape
        p = home / "Library/LaunchAgents/io.github.ctxport.ingest.plist"
        args = "".join(f"<string>{escape(a)}</string>" for a in cmd)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>io.github.ctxport.ingest</string>
<key>ProgramArguments</key><array>{args}</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>{escape(str(default_home() / 'ingest.log'))}</string>
</dict></plist>
""", encoding="utf-8")
        _run(["launchctl", "unload", str(p)])
        _run(["launchctl", "load", str(p)])
        return f"installed {p} (loaded now and at every login)"
    if sys.platform.startswith("linux"):
        p = home / ".config/systemd/user/ctxport-ingest.service"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[Unit]\nDescription=ctxport ingest server\n\n[Service]\nExecStart={' '.join(_systemd_quote(a) for a in cmd)}\nRestart=on-failure\n\n"
                     "[Install]\nWantedBy=default.target\n", encoding="utf-8")
        if _run(["systemctl", "--user", "daemon-reload"]) == 0:
            _run(["systemctl", "--user", "enable", "--now", "ctxport-ingest.service"])
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
            _run(["launchctl", "unload", str(p)])
            p.unlink()
            return f"removed {p}"
    elif sys.platform.startswith("linux"):
        p = home / ".config/systemd/user/ctxport-ingest.service"
        if p.exists():
            _run(["systemctl", "--user", "disable", "--now", "ctxport-ingest.service"])
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
