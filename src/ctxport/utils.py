"""Small helpers: time parsing, text extraction, zip/dir reading, managed blocks."""
from __future__ import annotations

import contextlib
import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

# ---------------------------------------------------------------- time

_ISO_FRACTION = re.compile(r"(\.\d+)")


def parse_time(value: Any) -> Optional[float]:
    """Return epoch seconds from seconds/ms/µs/ns numbers or ISO-8601 strings."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        while v > 1e11:  # ms, µs, ns
            v /= 1000.0
        return v
    if isinstance(value, dict):  # {"seconds":..., "nanos":...} protobuf-ish
        if "seconds" in value:
            return parse_time(float(value.get("seconds") or 0) + float(value.get("nanos") or 0) / 1e9)
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return parse_time(float(s))
    s = s.replace(" UTC", "Z").replace("Z", "+00:00")
    if "T" not in s and re.match(r"\d{4}-\d{2}-\d{2} \d", s):
        s = s.replace(" ", "T", 1)
    # Python 3.10 fromisoformat only accepts 3 or 6 fractional digits.
    m = _ISO_FRACTION.search(s)
    if m:
        frac = m.group(1)[1:]
        frac = (frac + "000000")[:6]
        s = s[: m.start()] + "." + frac + s[m.end():]
    # "+0000" -> "+00:00"
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%b %d, %Y, %I:%M:%S %p"):
            try:
                dt = datetime.strptime(s.split("+")[0], fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fmt_time(ts: Optional[float], with_time: bool = True) -> str:
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(ts).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def parse_since(value: Optional[str]) -> Optional[float]:
    """Accept '30d', '12h', or an ISO date."""
    if not value:
        return None
    m = re.fullmatch(r"(\d+)\s*([dhw])", value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return time.time() - n * {"h": 3600, "d": 86400, "w": 604800}[unit]
    return parse_time(value)


# ---------------------------------------------------------------- text

def text_of(content: Any, keys: tuple = ("text", "content", "parts", "value", "main_text")) -> str:
    """Best-effort readable text from strings / lists of parts / dicts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, list):
        parts = [text_of(c, keys) for c in content]
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        for k in keys:
            if k in content and content[k] not in (None, ""):
                return text_of(content[k], keys)
        return ""
    return ""


def strip_html(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>", "\n", s or "")
    s = re.sub(r"(?i)</(p|div|li|h\d|tr|pre)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def slugify(text: str, limit: int = 48) -> str:
    t = unicodedata.normalize("NFKC", text or "").strip().lower()
    t = re.sub(r"[^\w一-鿿-]+", "-", t, flags=re.UNICODE)
    t = re.sub(r"-{2,}", "-", t).strip("-_")
    return (t[:limit].rstrip("-") or "untitled")


def compact_json(obj: Any, limit: Optional[int] = None) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        s = str(obj)
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s


def load_json_lenient(data: bytes | str) -> Any:
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig", errors="replace")
    return json.loads(data)


def iter_jsonl(path: Path, warnings: Optional[list] = None) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                if warnings is not None:
                    warnings.append(f"{path.name}:{n}: invalid JSON line skipped")
                continue
            if isinstance(obj, dict):
                yield obj


_SECRET_KEYS = {"signature", "encrypted_content", "encrypted_reasoning", "thoughtSignature"}


def sanitize_raw(obj: Any, max_str: int = 2000) -> str:
    """Raw JSON for archival without binary payloads or encrypted reasoning."""

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if k in _SECRET_KEYS and isinstance(v, str):
                    out[k] = f"<omitted {len(v)} chars>"
                elif k in ("data", "image_url", "b64_json", "result") and isinstance(v, str) and len(v) > max_str \
                        and re.fullmatch(r"(data:[^,]*,)?[A-Za-z0-9+/=\s]+", v[:4000]):
                    out[k] = f"<omitted binary {len(v)} chars>"
                else:
                    out[k] = walk(v)
            return out
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o

    return compact_json(walk(obj))


def json_strings(obj: Any, min_len: int = 1) -> list:
    """All string leaves of a JSON value (for loosely-structured memory files)."""
    out: list = []

    def walk(o: Any) -> None:
        if isinstance(o, str):
            if len(o.strip()) >= min_len:
                out.append(o.strip())
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for x in o.values():
                walk(x)

    walk(obj)
    return out


# ---------------------------------------------------------------- unified zip/dir reading

class Bundle:
    """Read files the same way from a directory, a zip, or a single file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._zip: Optional[zipfile.ZipFile] = None
        if self.path.is_file() and zipfile.is_zipfile(self.path):
            self._zip = zipfile.ZipFile(self.path)

    def __enter__(self) -> "Bundle":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._zip:
            self._zip.close()

    @property
    def is_zip(self) -> bool:
        return self._zip is not None

    def names(self) -> list:
        if self._zip:
            return [n for n in self._zip.namelist() if not n.endswith("/")]
        if self.path.is_dir():
            return [p.relative_to(self.path).as_posix() for p in self.path.rglob("*") if p.is_file()]
        return [self.path.name]

    def find(self, pattern: str) -> list:
        rx = re.compile(pattern, re.I)
        return sorted(n for n in self.names() if rx.search(n))

    def read(self, name: str) -> bytes:
        if self._zip:
            return self._zip.read(name)
        if self.path.is_dir():
            return (self.path / name).read_bytes()
        return self.path.read_bytes()

    def read_json(self, name: str) -> Any:
        return load_json_lenient(self.read(name))

    def size(self, name: str) -> Optional[int]:
        try:
            if self._zip:
                return self._zip.getinfo(name).file_size
            if self.path.is_dir():
                return (self.path / name).stat().st_size
            return self.path.stat().st_size
        except Exception:
            return None

    @contextlib.contextmanager
    def materialize(self, name: str) -> Iterator[Path]:
        """Yield a real filesystem path for `name` (extracting from zip if needed)."""
        if not self._zip:
            yield (self.path / name) if self.path.is_dir() else self.path
            return
        tmp = tempfile.mkdtemp(prefix="ctxport-")
        try:
            target = Path(tmp) / Path(name).name
            target.write_bytes(self._zip.read(name))
            for suffix in ("-wal", "-shm"):
                if name + suffix in self._zip.namelist():
                    Path(str(target) + suffix).write_bytes(self._zip.read(name + suffix))
            yield target
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- sqlite (read-only on a copy)

@contextlib.contextmanager
def sqlite_copy(path: Path) -> Iterator[sqlite3.Connection]:
    """Copy a (possibly live) SQLite DB with its -wal/-shm into a temp dir and open it.

    Never touches the original database.
    """
    tmp = tempfile.mkdtemp(prefix="ctxport-db-")
    conn = None
    try:
        dst = Path(tmp) / Path(path).name
        shutil.copy2(path, dst)
        for suffix in ("-wal", "-shm", "-journal"):
            src = Path(str(path) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(dst) + suffix))
        conn = sqlite3.connect(str(dst))
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def table_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


# ---------------------------------------------------------------- signatures

def signature(path: Path) -> str:
    """Cheap change signature: mtime_ns+size (dir: latest mtime + total size + count)."""
    p = Path(path)
    try:
        if p.is_dir():
            latest = total = count = 0
            for f in p.rglob("*"):
                if f.is_file():
                    st = f.stat()
                    latest = max(latest, st.st_mtime_ns)
                    total += st.st_size
                    count += 1
            return f"d:{latest}:{total}:{count}"
        st = p.stat()
        sig = f"f:{st.st_mtime_ns}:{st.st_size}"
        for suffix in ("-wal",):
            w = Path(str(p) + suffix)
            if w.exists():
                ws = w.stat()
                sig += f":{ws.st_mtime_ns}:{ws.st_size}"
        return sig
    except OSError:
        return "missing"


# ---------------------------------------------------------------- managed blocks

BLOCK_BEGIN = "<!-- ctxport:begin"
BLOCK_END = "<!-- ctxport:end -->"
_BLOCK_RE = re.compile(r"<!-- ctxport:begin[^>]*-->.*?<!-- ctxport:end -->\n?", re.S)


def strip_managed(text: str) -> str:
    """Remove ctxport-generated blocks (used on re-import to avoid self-copying)."""
    return _BLOCK_RE.sub("", text or "")


def make_block(body: str, label: str = "") -> str:
    head = f"{BLOCK_BEGIN}{(' ' + label) if label else ''} — generated by ctxport; edits inside are overwritten -->"
    return f"{head}\n{body.rstrip()}\n{BLOCK_END}\n"


def write_managed(path: Path, body: str, label: str = "") -> dict:
    """Insert/replace the ctxport block in `path`. Backs up the original once."""
    path = Path(path).expanduser()
    block = make_block(body, label)
    existed = path.exists()
    old = path.read_text(encoding="utf-8", errors="replace") if existed else ""
    backup = Path(str(path) + ".ctxport.bak")
    if existed and not backup.exists() and not _BLOCK_RE.search(old):
        shutil.copy2(path, backup)  # the user's original, before ctxport ever touched it
    m = _BLOCK_RE.search(old)
    if m:
        # replace the first block, drop any stale duplicates after it
        new = old[: m.start()] + block + _BLOCK_RE.sub("", old[m.end():])
    else:
        if not old:
            sep = ""
        elif old.endswith("\n\n"):
            sep = ""
        elif old.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
        new = old + sep + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return {"path": str(path), "backup": str(backup) if backup.exists() else None, "created": not existed}


def home() -> Path:
    return Path(os.path.expanduser("~"))


def env_path(var: str, default: Path) -> Path:
    v = os.environ.get(var)
    return Path(v).expanduser() if v else default


def chunks(items: Iterable[str], size: int) -> Iterator[str]:
    buf = ""
    for it in items:
        if buf and len(buf) + len(it) + 2 > size:
            yield buf
            buf = ""
        while len(it) > size:
            yield it[:size]
            it = it[size:]
        buf = f"{buf}\n\n{it}" if buf else it
    if buf:
        yield buf
