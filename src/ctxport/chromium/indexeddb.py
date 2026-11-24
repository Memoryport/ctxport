"""Chromium IndexedDB (LevelDB backing store) and Local Storage readers — pure Python, read-only.

Usage:
    with ChromiumIndexedDB(path_to_*.indexeddb.leveldb) as idb:
        for rec in idb.records(database="chatbox", store="keyvaluepairs"):
            rec.key, rec.value
The directory (and the sibling *.indexeddb.blob dir) is copied to a temp dir first.
"""
from __future__ import annotations

import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .leveldb import read_db, read_varint, snappy_decompress
from .v8 import V8Error, deserialize, to_plain

OBJECT_STORE_DATA = 1
BLOB_ENTRY = 3


def _prefix(key: bytes) -> tuple:
    b = key[0]
    dl, ol, il = ((b >> 5) & 7) + 1, ((b >> 2) & 7) + 1, (b & 3) + 1
    i = 1
    db = int.from_bytes(key[i:i + dl], "little")
    i += dl
    os_ = int.from_bytes(key[i:i + ol], "little")
    i += ol
    ix = int.from_bytes(key[i:i + il], "little")
    i += il
    return db, os_, ix, i


def _str_with_len(buf: bytes, i: int) -> tuple:
    n, i = read_varint(buf, i)
    return buf[i:i + 2 * n].decode("utf-16-be", "replace"), i + 2 * n


def decode_idb_key(buf: bytes, i: int = 0) -> tuple:
    """Chromium encoded IDBKey: 0 null, 1 string, 2 date, 3 number, 4 array, 5 min, 6 binary."""
    t = buf[i]
    i += 1
    if t in (0, 5):
        return None, i
    if t == 1:
        return _str_with_len(buf, i)
    if t in (2, 3):
        v = struct.unpack("<d", buf[i:i + 8])[0]
        return (int(v) if v.is_integer() else v), i + 8
    if t == 4:
        n, i = read_varint(buf, i)
        out = []
        for _ in range(n):
            v, i = decode_idb_key(buf, i)
            out.append(v)
        return out, i
    if t == 6:
        n, i = read_varint(buf, i)
        return buf[i:i + n], i + n
    raise ValueError(f"unknown idb key type {t}")


@dataclass
class IDBRecord:
    database: str
    store: str
    key: Any
    value: Any


class ChromiumIndexedDB:
    def __init__(self, leveldb_dir: Path):
        self.src = Path(leveldb_dir)
        self.tmp = Path(tempfile.mkdtemp(prefix="ctxport-idb-"))
        self.warnings: list = []
        dst = self.tmp / self.src.name
        shutil.copytree(self.src, dst, ignore=shutil.ignore_patterns("LOCK"))
        blob_src = Path(str(self.src).replace(".leveldb", ".blob"))
        self.blob_dir = None
        if blob_src.is_dir() and blob_src != self.src:
            self.blob_dir = self.tmp / blob_src.name
            shutil.copytree(blob_src, self.blob_dir)
        self.kv = read_db(dst, self.warnings)
        self.databases: dict = {}   # id -> name
        self.stores: dict = {}      # (db_id, os_id) -> name
        self._parse_metadata()

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> "ChromiumIndexedDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _parse_metadata(self) -> None:
        for k, v in self.kv.items():
            if not k:
                continue
            try:
                db, os_, ix, i = _prefix(k)
            except IndexError:
                continue
            if db == 0 and os_ == 0 and ix == 0 and i < len(k) and k[i] == 201:  # DatabaseNameKey
                try:
                    _origin, j = _str_with_len(k, i + 1)
                    name, _ = _str_with_len(k, j)
                    dbid, _ = read_varint(v, 0)
                    self.databases[dbid] = name
                except Exception:
                    continue
            elif db and os_ == 0 and ix == 0 and i < len(k) and k[i] == 50:  # ObjectStoreMetaDataKey
                try:
                    osid, j = read_varint(k, i + 1)
                    if j < len(k) and k[j] == 0:  # name
                        self.stores[(db, osid)] = v.decode("utf-16-be", "replace")
                except Exception:
                    continue

    def _blob_path(self, db_id: int, blob_number: int) -> Optional[Path]:
        if not self.blob_dir:
            return None
        p = self.blob_dir / f"{db_id:x}" / f"{(blob_number >> 8) & 0xFF:02x}" / f"{blob_number:x}"
        return p if p.exists() else None

    def _blob_infos(self, db: int, os_: int, raw_key: bytes) -> list:
        """Blob entry list for a record: [(is_file, blob_number)]."""
        for k, v in self.kv.items():
            try:
                kdb, kos, kix, i = _prefix(k)
            except IndexError:
                continue
            if (kdb, kos, kix) == (db, os_, BLOB_ENTRY) and k[i:] == raw_key:
                out = []
                j = 0
                while j < len(v):
                    is_file = v[j] != 0
                    j += 1
                    num, j = read_varint(v, j)
                    _t, j = _str_with_len(v, j)
                    if is_file:
                        _fn, j = _str_with_len(v, j)
                        j += 8 if j + 8 <= len(v) else 0
                    else:
                        _sz, j = read_varint(v, j)
                    out.append((is_file, num))
                return out
        return []

    def _decode_value(self, db: int, os_: int, raw_key: bytes, v: bytes) -> Any:
        _ver, i = read_varint(v, 0)
        body = v[i:]
        if body[:3] == b"\xff\x11\x01":  # wrapped: real value lives in a blob file
            j = 3
            _size, j = read_varint(body, j)
            idx, j = read_varint(body, j)
            infos = self._blob_infos(db, os_, raw_key)
            if idx < len(infos):
                p = self._blob_path(db, infos[idx][1])
                if p:
                    body = p.read_bytes()
                    if body[:3] == b"\xff\x11\x02":
                        body = snappy_decompress(body[3:])
                    return deserialize(body)
            raise V8Error("wrapped value blob not found")
        if body[:3] == b"\xff\x11\x02":
            body = snappy_decompress(body[3:])
        return deserialize(body)

    def records(self, database: Optional[str] = None, store: Optional[str] = None,
                plain: bool = True) -> Iterator[IDBRecord]:
        for k, v in self.kv.items():
            if not k:
                continue
            try:
                db, os_, ix, i = _prefix(k)
            except IndexError:
                continue
            if ix != OBJECT_STORE_DATA or not db or not os_:
                continue
            dname = self.databases.get(db, str(db))
            sname = self.stores.get((db, os_), str(os_))
            if database is not None and dname != database:
                continue
            if store is not None and sname != store:
                continue
            try:
                key, _ = decode_idb_key(k, i)
                val = self._decode_value(db, os_, k[i:], v)
            except Exception as e:
                self.warnings.append(f"{dname}/{sname}: record skipped: {type(e).__name__}: {e}")
                continue
            yield IDBRecord(dname, sname, key, to_plain(val) if plain else val)

    def summary(self) -> dict:
        """{database: {store: count}} — structure only."""
        out: dict = {}
        for (db, os_), name in self.stores.items():
            out.setdefault(self.databases.get(db, str(db)), {})[name] = 0
        for k in self.kv:
            try:
                db, os_, ix, _ = _prefix(k)
            except IndexError:
                continue
            if ix == OBJECT_STORE_DATA and (db, os_) in self.stores:
                out[self.databases.get(db, str(db))][self.stores[(db, os_)]] += 1
        return out


def read_local_storage(leveldb_dir: Path, origin_prefix: str = "") -> dict:
    """Chromium 'Local Storage/leveldb': {(origin, key): value_str}."""
    tmp = Path(tempfile.mkdtemp(prefix="ctxport-ls-"))
    try:
        dst = tmp / "ls"
        shutil.copytree(leveldb_dir, dst, ignore=shutil.ignore_patterns("LOCK"))
        kv = read_db(dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out: dict = {}
    for k, v in kv.items():
        if not k.startswith(b"_") or b"\x00" not in k:
            continue
        origin, rest = k[1:].split(b"\x00", 1)
        o = origin.decode("utf-8", "replace")
        if origin_prefix and not o.startswith(origin_prefix):
            continue
        key = _ls_str(rest)
        val = _ls_str(v)
        if key is not None and val is not None:
            out[(o, key)] = val
    return out


def _ls_str(b: bytes) -> Optional[str]:
    if not b:
        return ""
    if b[0] == 0:
        return b[1:].decode("utf-16-le", "replace")
    if b[0] == 1:
        return b[1:].decode("latin-1")
    return None
