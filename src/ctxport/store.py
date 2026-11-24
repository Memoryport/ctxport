"""Local SQLite store with FTS5 trigram full-text search (falls back to LIKE)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

from .models import Conversation, MemoryItem, Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  key TEXT PRIMARY KEY, platform TEXT, path TEXT, signature TEXT, imported_at REAL,
  conversations INTEGER DEFAULT 0, warnings INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY, platform TEXT NOT NULL, native_id TEXT NOT NULL, title TEXT,
  created_at REAL, updated_at REAL, project TEXT, model TEXT, source_path TEXT, source_key TEXT,
  message_count INTEGER DEFAULT 0, user_count INTEGER DEFAULT 0, char_count INTEGER DEFAULT 0,
  branch_count INTEGER DEFAULT 0, meta TEXT, imported_at REAL);
CREATE INDEX IF NOT EXISTS conv_platform ON conversations(platform, updated_at);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  branch INTEGER DEFAULT 0, idx INTEGER, msg_id TEXT, parent_id TEXT, role TEXT, kind TEXT,
  text TEXT, model TEXT, created_at REAL, meta TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS msg_conv ON messages(conversation_id, branch, idx);
CREATE TABLE IF NOT EXISTS memories(
  id TEXT PRIMARY KEY, text TEXT NOT NULL, platform TEXT, category TEXT, source_ref TEXT,
  confidence REAL, status TEXT, scope TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS extracted(
  conversation_id TEXT, method TEXT, extracted_at REAL, PRIMARY KEY(conversation_id, method));
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages WHEN new.kind IN ('message','artifact') BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages WHEN old.kind IN ('message','artifact') BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text); END;
CREATE TRIGGER IF NOT EXISTS messages_au_del AFTER UPDATE ON messages WHEN old.kind IN ('message','artifact') BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text); END;
CREATE TRIGGER IF NOT EXISTS messages_au_ins AFTER UPDATE ON messages WHEN new.kind IN ('message','artifact') BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
"""

DIALOGUE_KINDS = ("message",)
# Only these kinds go into the full-text index; tool output and reasoning are searched with LIKE when asked for.
FTS_KINDS = ("message", "artifact")
# Tool output longer than this keeps only its first and last TOOL_KEEP characters (CTXPORT_KEEP_FULL_TOOLS=1 or
# --keep-full-tools keeps everything). Tool output is excluded from migrations, extraction and default search, and
# on coding assistants it is most of the database.
TOOL_KEEP = 2048
TOOL_KINDS = ("tool_call", "tool_result")


def trim_tool_text(text: str, keep: int = TOOL_KEEP) -> str:
    if not text or len(text) <= 2 * keep + 200:
        return text
    omitted = len(text) - 2 * keep
    return f"{text[:keep]}\n…[ctxport: {omitted:,} characters omitted]…\n{text[-keep:]}"

# Database layout version, kept in SQLite's `PRAGMA user_version`. Version 1 is the layout in SCHEMA above
# (databases from 0.1.0 have user_version 0 and already match it). To change the layout:
#   1. bump SCHEMA_VERSION,
#   2. add MIGRATIONS[new_version] = SQL that upgrades a database of the previous version (ALTER TABLE ...),
#   3. update SCHEMA so brand-new databases are created in the new layout directly.
# Migrations run in order, each in its own transaction, the next time the store is opened.
SCHEMA_VERSION = 2
MIGRATIONS: dict = {
    # 2: the full-text index covers dialogue only (FTS_KINDS); the triggers are recreated by FTS_SCHEMA and the
    #    index is rebuilt once after the migration.
    2: """
DROP TRIGGER IF EXISTS messages_ai;
DROP TRIGGER IF EXISTS messages_ad;
DROP TRIGGER IF EXISTS messages_au;
""",
}


class NewerDatabase(RuntimeError):
    """The database was written by a newer ctxport whose layout this version doesn't know."""


def default_home() -> Path:
    return Path(os.environ.get("CTXPORT_HOME") or Path.home() / ".ctxport").expanduser()


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # not our file, or a filesystem without POSIX modes (Windows): best effort
        pass


def ensure_private_dir(path: Path) -> Path:
    """Create `path` (and parents) and make it accessible only to the current user (0700 on POSIX).

    Chat history is personal data; other local accounts must not be able to read it."""
    path = Path(path)
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    home = default_home()
    try:
        rel = path.resolve().relative_to(home.resolve())
    except (ValueError, OSError):
        # a custom location (e.g. --db /somewhere/x.db): never change modes of a directory we didn't create
        if not existed:
            _chmod(path, 0o700)
        return path
    # inside the ctxport home: tighten the home itself and every directory down to `path`
    d = home
    _chmod(d, 0o700)
    for part in rel.parts:
        d = d / part
        _chmod(d, 0o700)
    return path


def private_write_text(path: Path, text: str) -> None:
    """Atomically write `text` to `path` with 0600 permissions (temp file in the same dir + os.replace)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _chmod(path, 0o600)


def _split_sql(script: str) -> list:
    """Split a migration script into statements (executescript would commit outside our transaction)."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


class AmbiguousId(ValueError):
    pass


def _j(obj) -> str:
    return json.dumps(obj or {}, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path: Optional[Path] = None, store_raw: Optional[bool] = None):
        if path is None:
            path = default_home() / "ctxport.db"
        self.path = Path(path)
        ensure_private_dir(self.path.parent)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        # Raw source JSON roughly triples the DB size; keep it only when asked (--keep-raw / CTXPORT_STORE_RAW=1).
        self.store_raw = store_raw if store_raw is not None else os.environ.get("CTXPORT_STORE_RAW") == "1"
        self.keep_full_tools = os.environ.get("CTXPORT_KEEP_FULL_TOOLS") == "1"
        self._rebuild_fts = False
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self.fts = self._init_fts()
        if self.fts and self._rebuild_fts:
            self.rebuild_fts()
        self.db.commit()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                _chmod(p, 0o600)

    def schema_version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        v = self.schema_version()
        if v > SCHEMA_VERSION:
            self.db.close()
            raise NewerDatabase(f"{self.path} was created by a newer ctxport (database version {v}, this ctxport "
                                f"supports up to {SCHEMA_VERSION}). Upgrade ctxport: pip install -U ctxport")
        fresh = not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone()
        if fresh or v == 0:
            # new database, or one from 0.1.0 (no version stamp yet, layout of version 1)
            self.db.executescript(SCHEMA)
            v = SCHEMA_VERSION if fresh else 1
            self.db.execute(f"PRAGMA user_version = {int(v)}")
            self.db.commit()
        for target in range(v + 1, SCHEMA_VERSION + 1):
            if target == 2:
                self._rebuild_fts = True
            try:
                self.db.execute("BEGIN")
                for stmt in _split_sql(MIGRATIONS[target]):
                    self.db.execute(stmt)
                self.db.execute(f"PRAGMA user_version = {int(target)}")
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def rebuild_fts(self) -> None:
        """Re-index dialogue messages only (FTS_KINDS)."""
        kinds = ",".join(f"'{k}'" for k in FTS_KINDS)
        self.db.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
        self.db.execute(f"INSERT INTO messages_fts(rowid, text) SELECT id, text FROM messages WHERE kind IN ({kinds})")
        self.db.commit()

    def _init_fts(self) -> bool:
        try:
            self.db.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ sources
    def source_signature(self, key: str) -> Optional[str]:
        r = self.db.execute("SELECT signature FROM sources WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_source(self, key: str, platform: str, path: str, sig: str, n_conv: int, n_warn: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sources(key,platform,path,signature,imported_at,conversations,warnings) VALUES(?,?,?,?,?,?,?)",
            (key, platform, path, sig, time.time(), n_conv, n_warn),
        )
        self.db.commit()

    # ------------------------------------------------------------ conversations
    def upsert_conversation(self, conv: Conversation, source_key: Optional[str] = None) -> None:
        conv.finalize()
        cid = conv.id
        old = self.db.execute("SELECT updated_at, message_count FROM conversations WHERE id=?", (cid,)).fetchone()
        self.db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        main = conv.messages
        user_count = sum(1 for m in main if m.role == "user" and m.kind == "message")
        chars = sum(len(m.text or "") for m in main if m.kind == "message")
        self.db.execute(
            """INSERT OR REPLACE INTO conversations(id,platform,native_id,title,created_at,updated_at,project,model,
               source_path,source_key,message_count,user_count,char_count,branch_count,meta,imported_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, conv.platform, conv.native_id, conv.title, conv.created_at, conv.updated_at, conv.project, conv.model,
             conv.source_path, source_key, len(main), user_count, chars, len(conv.branches), _j(conv.meta), time.time()),
        )
        rows = []
        for b, msgs in enumerate([main] + list(conv.branches)):
            for i, m in enumerate(msgs):
                text = m.text or ""
                if m.kind in TOOL_KINDS and not self.keep_full_tools:
                    text = trim_tool_text(text)
                rows.append((cid, b, i, m.id, m.parent_id, m.role, m.kind, text, m.model, m.created_at,
                             _j(m.meta), m.raw if self.store_raw else None))
        self.db.executemany(
            "INSERT INTO messages(conversation_id,branch,idx,msg_id,parent_id,role,kind,text,model,created_at,meta,raw)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        if old is None or old["updated_at"] != conv.updated_at or old["message_count"] != len(main):
            self.db.execute("DELETE FROM extracted WHERE conversation_id=?", (cid,))

    def commit(self) -> None:
        self.db.commit()

    def conversation_rows(self, platform: Optional[str] = None, limit: Optional[int] = None,
                          since: Optional[float] = None, order: str = "updated_at DESC") -> list:
        q = "SELECT * FROM conversations WHERE 1=1"
        args: list = []
        if platform:
            plats = [p.strip() for p in platform.split(",") if p.strip()]
            q += f" AND platform IN ({','.join('?' * len(plats))})"
            args += plats
        if since:
            q += " AND COALESCE(updated_at, created_at, 0) >= ?"
            args.append(since)
        q += f" ORDER BY {order}"
        if limit:
            q += " LIMIT ?"
            args.append(int(limit))
        return self.db.execute(q, args).fetchall()

    def find_conversation_id(self, ident: str) -> Optional[str]:
        r = self.db.execute("SELECT id FROM conversations WHERE id=?", (ident,)).fetchone()
        if r:
            return r[0]
        rows = self.db.execute(
            "SELECT id FROM conversations WHERE native_id LIKE ? OR id LIKE ? LIMIT 2", (ident + "%", ident + "%")
        ).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def get_conversation(self, cid: str, include_raw: bool = False) -> Optional[Conversation]:
        r = self.db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if not r:
            return None
        conv = Conversation(platform=r["platform"], native_id=r["native_id"], title=r["title"] or "",
                            created_at=r["created_at"], updated_at=r["updated_at"], project=r["project"],
                            model=r["model"], source_path=r["source_path"], meta=json.loads(r["meta"] or "{}"))
        branches: dict = {}
        for m in self.db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY branch, idx", (cid,)):
            msg = Message(role=m["role"], text=m["text"], id=m["msg_id"], parent_id=m["parent_id"],
                          created_at=m["created_at"], model=m["model"], kind=m["kind"],
                          meta=json.loads(m["meta"] or "{}"), raw=m["raw"] if include_raw else None)
            branches.setdefault(m["branch"], []).append(msg)
        conv.messages = branches.pop(0, [])
        conv.branches = [branches[k] for k in sorted(branches)]
        return conv

    def iter_conversations(self, platform: Optional[str] = None, limit: Optional[int] = None,
                           since: Optional[float] = None, include_raw: bool = False,
                           order: str = "updated_at DESC") -> Iterable[Conversation]:
        for r in self.conversation_rows(platform, limit, since, order):
            c = self.get_conversation(r["id"], include_raw)
            if c:
                yield c

    # ------------------------------------------------------------ search
    def search(self, query: str, platform: Optional[str] = None, limit: int = 20,
               include_tools: bool = False, include_reasoning: bool = False) -> list:
        kinds = ["message", "artifact"]
        if include_tools:
            kinds += ["tool_call", "tool_result", "attachment"]
        if include_reasoning:
            kinds.append("reasoning")
        terms = [t for t in query.split() if t]
        if not terms:
            return []
        kind_sql = f"m.kind IN ({','.join('?' * len(kinds))})"
        args: list = []
        # the index holds dialogue only; tool output / reasoning searches scan with LIKE
        use_fts = self.fts and all(len(t) >= 3 for t in terms) and set(kinds) <= set(FTS_KINDS)
        if use_fts:
            fts_q = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
            sql = ("SELECT m.*, c.title, c.platform FROM messages_fts f JOIN messages m ON m.id=f.rowid "
                   "JOIN conversations c ON c.id=m.conversation_id WHERE messages_fts MATCH ? AND " + kind_sql)
            args = [fts_q] + kinds
        else:
            like = " AND ".join("m.text LIKE ? ESCAPE '\\'" for _ in terms)
            sql = ("SELECT m.*, c.title, c.platform FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                   f"WHERE {like} AND " + kind_sql)
            args = ["%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in terms] + kinds
        if platform:
            sql += " AND c.platform=?"
            args.append(platform)
        sql += " ORDER BY COALESCE(m.created_at, c.updated_at) DESC LIMIT ?"
        args.append(int(limit))
        try:
            return self.db.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            if use_fts:
                self.fts = False
                try:
                    return self.search(query, platform, limit, include_tools, include_reasoning)
                finally:
                    self.fts = True
            raise

    # ------------------------------------------------------------ memories
    def upsert_memory(self, mem: MemoryItem) -> str:
        """Insert or merge; returns 'new' | 'updated' | 'kept'."""
        if not mem.text:
            return "kept"
        r = self.db.execute("SELECT status, confidence FROM memories WHERE id=?", (mem.id,)).fetchone()
        now = time.time()
        if r is None:
            self.db.execute(
                "INSERT INTO memories(id,text,platform,category,source_ref,confidence,status,scope,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (mem.id, mem.text, mem.platform, mem.category, mem.source_ref, mem.confidence, mem.status, mem.scope,
                 mem.created_at, now))
            return "new"
        if r["status"] == "rejected":
            return "kept"
        if r["status"] == "candidate" and mem.status == "accepted":
            self.db.execute("UPDATE memories SET status='accepted', confidence=MAX(confidence,?), updated_at=? WHERE id=?",
                            (mem.confidence, now, mem.id))
            return "updated"
        return "kept"

    def memories(self, status: Optional[Iterable[str]] = ("accepted",), scope: Optional[str] = None,
                 platform: Optional[str] = None) -> list:
        q = "SELECT * FROM memories WHERE 1=1"
        args: list = []
        if status:
            st = list(status)
            q += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        if scope:
            q += " AND scope=?"
            args.append(scope)
        if platform:
            q += " AND platform=?"
            args.append(platform)
        q += " ORDER BY scope, category, created_at"
        return [MemoryItem(id=r["id"], text=r["text"], platform=r["platform"], category=r["category"],
                           source_ref=r["source_ref"], confidence=r["confidence"], status=r["status"],
                           scope=r["scope"], created_at=r["created_at"])
                for r in self.db.execute(q, args)]

    def resolve_memory_id(self, mid: str) -> Optional[str]:
        """Exact id, or a prefix that matches exactly one memory. None if nothing matches.

        Raises AmbiguousId when a prefix matches several memories, so `memories rm a` can never hit many rows.
        The prefix is compared literally (no LIKE wildcards)."""
        mid = (mid or "").strip()
        if not mid:
            return None
        if self.db.execute("SELECT 1 FROM memories WHERE id=?", (mid,)).fetchone():
            return mid
        rows = self.db.execute("SELECT id FROM memories WHERE substr(id, 1, ?)=? LIMIT 6",
                               (len(mid), mid)).fetchall()
        if len(rows) > 1:
            shown = ", ".join(r[0] for r in rows[:5])
            raise AmbiguousId(f"id prefix {mid!r} matches several memories ({shown}{', …' if len(rows) > 5 else ''});"
                              " use more characters")
        return rows[0][0] if rows else None

    def set_memory_status(self, mid: str, status: str) -> int:
        rid = self.resolve_memory_id(mid)
        if rid is None:
            return 0
        cur = self.db.execute("UPDATE memories SET status=?, updated_at=? WHERE id=?", (status, time.time(), rid))
        self.db.commit()
        return cur.rowcount

    def update_memory_text(self, mid: str, text: str, category: Optional[str] = None, status: str = "accepted") -> None:
        r = self.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        if not r:
            return
        new = MemoryItem(text=text, platform=r["platform"], category=category or r["category"],
                         source_ref=r["source_ref"], confidence=1.0, status=status, scope=r["scope"])
        if new.id != mid:
            self.db.execute("UPDATE memories SET status='rejected', updated_at=? WHERE id=?", (time.time(), mid))
            self.db.execute("DELETE FROM memories WHERE id=? AND status!='rejected'", (new.id,))
            self.upsert_memory(new)
        else:
            self.db.execute("UPDATE memories SET status=?, category=?, updated_at=? WHERE id=?",
                            (status, new.category, time.time(), mid))

    def delete_memory(self, mid: str) -> int:
        rid = self.resolve_memory_id(mid)
        if rid is None:
            return 0
        cur = self.db.execute("DELETE FROM memories WHERE id=?", (rid,))
        self.db.commit()
        return cur.rowcount

    # ------------------------------------------------------------ extraction bookkeeping
    def is_extracted(self, cid: str, method: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM extracted WHERE conversation_id=? AND method=?", (cid, method)).fetchone())

    def mark_extracted(self, cid: str, method: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO extracted VALUES(?,?,?)", (cid, method, time.time()))

    # ------------------------------------------------------------ stats
    def stats(self) -> dict:
        out: dict = {"platforms": {}, "memories": {}}
        for r in self.db.execute(
                "SELECT platform, COUNT(*) n, SUM(message_count) msgs, SUM(branch_count) br, MIN(created_at) lo,"
                " MAX(updated_at) hi FROM conversations GROUP BY platform ORDER BY platform"):
            top = self.db.execute(
                "SELECT model, COUNT(*) c FROM conversations WHERE platform=? AND model IS NOT NULL AND model!=''"
                " GROUP BY model ORDER BY c DESC LIMIT 3", (r["platform"],)).fetchall()
            allm = self.db.execute(
                "SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.platform=?",
                (r["platform"],)).fetchone()[0]
            out["platforms"][r["platform"]] = {
                "conversations": r["n"], "messages": r["msgs"] or 0, "all_records": allm, "branches": r["br"] or 0,
                "first": r["lo"], "last": r["hi"], "top_models": [(t[0], t[1]) for t in top]}
        for r in self.db.execute("SELECT status, COUNT(*) FROM memories GROUP BY status"):
            out["memories"][r[0]] = r[1]
        out["warnings"] = self.db.execute("SELECT COALESCE(SUM(warnings),0) FROM sources").fetchone()[0]
        return out

    # ------------------------------------------------------------ disk usage
    def size_report(self) -> dict:
        """File size, free pages, and stored text bytes per platform and kind (no dbstat needed)."""
        page = self.db.execute("PRAGMA page_size").fetchone()[0]
        pages = self.db.execute("PRAGMA page_count").fetchone()[0]
        free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
        files = {}
        for suffix in ("", "-wal"):
            f = Path(str(self.path) + suffix)
            if f.exists():
                files[suffix or "db"] = f.stat().st_size
        by: dict = {}
        for r in self.db.execute(
                "SELECT c.platform, m.kind, COUNT(*) n, SUM(LENGTH(CAST(m.text AS BLOB))) b,"
                " SUM(LENGTH(CAST(COALESCE(m.raw, '') AS BLOB))) raw"
                " FROM messages m JOIN conversations c ON c.id=m.conversation_id GROUP BY c.platform, m.kind"):
            d = by.setdefault(r["platform"], {})
            group = "dialogue" if r["kind"] in FTS_KINDS else ("tools" if r["kind"] in TOOL_KINDS else "other")
            d[group] = d.get(group, 0) + (r["b"] or 0)
            if r["raw"]:
                d["raw"] = d.get("raw", 0) + r["raw"]
        trimmable = self.db.execute(
            f"SELECT COUNT(*) FROM messages WHERE kind IN ({','.join('?' * len(TOOL_KINDS))}) AND LENGTH(text) > ?",
            (*TOOL_KINDS, 2 * TOOL_KEEP + 200)).fetchone()[0]
        return {"path": str(self.path), "files": files, "page_size": page, "pages": pages, "free_pages": free,
                "free_ratio": (free / pages) if pages else 0.0, "text_bytes": by, "trimmable_tool_messages": trimmable}

    def trim_tool_output(self) -> int:
        """Apply TOOL_KEEP trimming to tool output stored by older versions. Returns rows changed."""
        n = 0
        rows = self.db.execute(
            f"SELECT id, text FROM messages WHERE kind IN ({','.join('?' * len(TOOL_KINDS))}) AND LENGTH(text) > ?",
            (*TOOL_KINDS, 2 * TOOL_KEEP + 200)).fetchall()
        for r in rows:
            self.db.execute("UPDATE messages SET text=? WHERE id=?", (trim_tool_text(r["text"]), r["id"]))
            n += 1
        self.db.commit()
        return n

    def compact(self) -> None:
        """Merge full-text index segments, give free pages back to the file system and shrink the WAL."""
        if self.fts:
            self.db.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
            self.db.commit()
        self.db.execute("VACUUM")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
