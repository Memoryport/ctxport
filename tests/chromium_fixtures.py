"""Build synthetic Chromium IndexedDB / Local Storage LevelDB directories (no real data)."""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


# ---------------------------------------------------------------- V8 / Blink serialization (subset)

def _v8(v) -> bytes:
    if v is None:
        return b"0"
    if v is True:
        return b"T"
    if v is False:
        return b"F"
    if isinstance(v, int):
        return b"I" + varint((v << 1) ^ (v >> 63))
    if isinstance(v, float):
        return b"N" + struct.pack("<d", v)
    if isinstance(v, str):
        try:
            raw = v.encode("latin-1")
            return b'"' + varint(len(raw)) + raw
        except UnicodeEncodeError:
            raw = v.encode("utf-16-le")
            pad = b"\x00" if False else b""
            return pad + b"c" + varint(len(raw)) + raw
    if isinstance(v, list):
        body = b"".join(_v8(x) for x in v)
        return b"A" + varint(len(v)) + body + b"$" + varint(0) + varint(len(v))
    if isinstance(v, dict):
        body = b"".join(_v8(k) + _v8(x) for k, x in v.items())
        return b"o" + body + b"{" + varint(len(v))
    raise TypeError(type(v))


def blink_value(v) -> bytes:
    """IDB value: idb version varint + Blink envelope (v21 with trailer) + V8 header + value."""
    return varint(1) + b"\xff\x15\xfe" + b"\x00" * 12 + b"\xff\x0f" + _v8(v)


# ---------------------------------------------------------------- IndexedDB keys

def _sw(s: str) -> bytes:
    return varint(len(s)) + s.encode("utf-16-be")


def prefix(db: int, os_: int, ix: int) -> bytes:
    return bytes([0]) + bytes([db]) + bytes([os_]) + bytes([ix])


def db_name_key(origin: str, name: str) -> bytes:
    return prefix(0, 0, 0) + bytes([201]) + _sw(origin) + _sw(name)


def store_name_key(db: int, os_: int) -> bytes:
    return prefix(db, 0, 0) + bytes([50]) + varint(os_) + bytes([0])


def data_key(db: int, os_: int, key) -> bytes:
    if isinstance(key, str):
        enc = bytes([1]) + _sw(key)
    else:
        enc = bytes([3]) + struct.pack("<d", float(key))
    return prefix(db, os_, 1) + enc


# ---------------------------------------------------------------- LevelDB writers

def write_log(path: Path, records: list, seq: int = 1) -> None:
    """records: [(key, value_or_None)] as one write batch, split into 32 KiB-block fragments."""
    batch = bytearray(struct.pack("<QI", seq, len(records)))
    for k, v in records:
        if v is None:
            batch += b"\x00" + varint(len(k)) + k
        else:
            batch += b"\x01" + varint(len(k)) + k + varint(len(v)) + v
    out = bytearray()
    data = bytes(batch)
    first = True
    while data or first:
        left = 32768 - (len(out) % 32768)
        if left < 7:
            out += b"\x00" * left
            continue
        frag = data[: left - 7]
        data = data[len(frag):]
        if first and not data:
            t = 1
        elif first:
            t = 2
        elif not data:
            t = 4
        else:
            t = 3
        first = False
        out += struct.pack("<IHB", zlib.crc32(frag) & 0xFFFFFFFF, len(frag), t) + frag
    path.write_bytes(bytes(out))


def snappy_literal(data: bytes) -> bytes:
    """Valid snappy stream made of literals only (enough to exercise the decoder)."""
    out = bytearray(varint(len(data)))
    i = 0
    while i < len(data):
        chunk = data[i:i + 60]
        out.append((len(chunk) - 1) << 2)
        out += chunk
        i += len(chunk)
    return bytes(out)


def write_table(path: Path, records: list, seq: int = 100, compress: bool = True) -> None:
    """Minimal SSTable: one data block (snappy) + index block + footer."""
    def block(entries):
        b = bytearray()
        for k, v in entries:
            b += varint(0) + varint(len(k)) + varint(len(v)) + k + v
        b += struct.pack("<I", 0) + struct.pack("<I", 1)
        return bytes(b)

    entries = []
    for n, (k, v) in enumerate(sorted(records, key=lambda kv: kv[0])):
        tail = ((seq + n) << 8) | (1 if v is not None else 0)
        entries.append((k + struct.pack("<Q", tail), v if v is not None else b""))
    data = block(entries)
    body = snappy_literal(data) if compress else data
    out = bytearray(body) + bytes([1 if compress else 0]) + b"\x00" * 4
    index_handle = varint(0) + varint(len(body))
    index = block([(entries[-1][0], index_handle)])
    index_off = len(out)
    out += index + b"\x00" * 5
    meta_off = len(out)
    meta = block([])
    out += meta + b"\x00" * 5
    footer = varint(meta_off) + varint(len(meta)) + varint(index_off) + varint(len(index))
    footer += b"\x00" * (40 - len(footer)) + b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"
    out += footer
    path.write_bytes(bytes(out))


def make_idb(dir_: Path, origin: str, dbname: str, stores: dict, split_table: bool = True) -> Path:
    """stores: {store_name: {key: value}} → <dir>/file__0.indexeddb.leveldb"""
    d = dir_ / "file__0.indexeddb.leveldb"
    d.mkdir(parents=True, exist_ok=True)
    meta = [(db_name_key(origin, dbname), varint(1))]
    data = []
    for i, (sname, items) in enumerate(stores.items(), start=1):
        meta.append((store_name_key(1, i), sname.encode("utf-16-be")))
        for k, v in items.items():
            data.append((data_key(1, i, k), blink_value(v)))
    if split_table and len(data) > 1:
        half = len(data) // 2
        write_table(d / "000005.ldb", meta + data[:half], seq=1)
        write_log(d / "000006.log", data[half:], seq=1000)
    else:
        write_log(d / "000003.log", meta + data)
    (d / "CURRENT").write_text("MANIFEST-000001\n")
    return d


def make_local_storage(dir_: Path, origin: str, items: dict) -> Path:
    d = dir_ / "Local Storage" / "leveldb"
    d.mkdir(parents=True, exist_ok=True)
    recs = [(b"_" + origin.encode() + b"\x00\x01" + k.encode("latin-1"), b"\x00" + v.encode("utf-16-le"))
            for k, v in items.items()]
    write_log(d / "000003.log", recs)
    return d


# ---------------------------------------------------------------- app layouts

def chatbox_app(tmp: Path) -> Path:
    root = tmp / "xyz.chatboxapp.app"
    sess = {"id": "loc1", "name": "本地会话", "type": "chat",
            "messages": [{"id": "m1", "role": "user", "contentParts": [{"type": "text", "text": "本地问题 local question"}], "timestamp": 1767225600000},
                         {"id": "m2", "role": "assistant", "contentParts": [{"type": "text", "text": "local answer"}], "timestamp": 1767225601000}]}
    make_idb(root / "IndexedDB", "file://", "chatboxstore", {"keyvaluepairs": {
        "chat-sessions-list": json.dumps([{"id": "loc1", "name": "本地会话"}]),
        "session:loc1": json.dumps(sess, ensure_ascii=False),
        "myCopilots": json.dumps([{"name": "Tutor", "prompt": "You are a calm tutor"}]),
    }})
    (root / "config.json").write_text(json.dumps({"settings": {"agent-memories": ["Likes green tea"]}}), encoding="utf-8")
    return root


def cherry_app(tmp: Path) -> Path:
    root = tmp / "CherryStudio"
    make_idb(root / "IndexedDB", "file://", "CherryStudio", {
        "topics": {"tp1": {"id": "tp1", "messages": [
            {"id": "u1", "role": "user", "topicId": "tp1", "createdAt": "2026-05-18T10:00:00.000Z", "blocks": ["b1"]},
            {"id": "a1", "role": "assistant", "topicId": "tp1", "createdAt": "2026-05-18T10:00:05.000Z", "blocks": ["b2", "b3"],
             "model": {"id": "qwen-x", "name": "Qwen"}}]}},
        "message_blocks": {"b1": {"id": "b1", "messageId": "u1", "type": "main_text", "content": "樱桃问题"},
                           "b2": {"id": "b2", "messageId": "a1", "type": "thinking", "content": "hmm", "thinking_millsec": 12.5},
                           "b3": {"id": "b3", "messageId": "a1", "type": "main_text", "content": "樱桃回答"}},
        "files": {},
    })
    persist = {"assistants": json.dumps({"assistants": [{"id": "as", "name": "默认助手", "prompt": "Be precise",
                                                          "topics": [{"id": "tp1", "name": "樱桃话题", "createdAt": "2026-05-18T10:00:00Z"}]}]})}
    make_local_storage(root, "file://", {"persist:cherry-studio": json.dumps(persist, ensure_ascii=False)})
    return root
