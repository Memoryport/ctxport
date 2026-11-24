"""Read-only LevelDB reader (log + .ldb/.sst tables, snappy) in pure Python.

Only what is needed to read Chromium's IndexedDB / Local Storage: every record from every
file, newest sequence number wins. The directory is copied to a temp dir first by callers.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

BLOCK_SIZE = 32768


def read_varint(buf: bytes, i: int) -> tuple:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


# ---------------------------------------------------------------- snappy

def snappy_decompress(data: bytes) -> bytes:
    n, i = read_varint(data, 0)
    out = bytearray()
    ln = len(data)
    while i < ln:
        tag = data[i]
        i += 1
        t = tag & 3
        if t == 0:  # literal
            size = tag >> 2
            if size >= 60:
                nb = size - 59
                size = int.from_bytes(data[i:i + nb], "little")
                i += nb
            size += 1
            out += data[i:i + size]
            i += size
            continue
        if t == 1:
            length = 4 + ((tag >> 2) & 7)
            offset = ((tag >> 5) << 8) | data[i]
            i += 1
        elif t == 2:
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[i:i + 2], "little")
            i += 2
        else:
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[i:i + 4], "little")
            i += 4
        if offset <= 0 or offset > len(out):
            raise ValueError("bad snappy offset")
        start = len(out) - offset
        if offset >= length:
            out += out[start:start + length]
        else:
            for k in range(length):
                out.append(out[start + k])
    if len(out) != n:
        raise ValueError("snappy length mismatch")
    return bytes(out)


# ---------------------------------------------------------------- records

@dataclass
class Record:
    key: bytes
    value: Optional[bytes]
    seq: int
    deleted: bool


def _batch_records(batch: bytes) -> Iterator[Record]:
    if len(batch) < 12:
        return
    seq, count = struct.unpack_from("<QI", batch, 0)
    i = 12
    for n in range(count):
        if i >= len(batch):
            return
        tag = batch[i]
        i += 1
        klen, i = read_varint(batch, i)
        key = batch[i:i + klen]
        i += klen
        if tag == 1:
            vlen, i = read_varint(batch, i)
            val = batch[i:i + vlen]
            i += vlen
            yield Record(key, val, seq + n, False)
        elif tag == 0:
            yield Record(key, None, seq + n, True)
        else:
            return


def read_log(path: Path) -> Iterator[Record]:
    """Write-ahead log: 32 KiB blocks of fragments (FULL/FIRST/MIDDLE/LAST) holding write batches."""
    data = Path(path).read_bytes()
    pending = bytearray()
    i = 0
    while i + 7 <= len(data):
        block_left = BLOCK_SIZE - (i % BLOCK_SIZE)
        if block_left < 7:
            i += block_left
            continue
        length, rtype = struct.unpack_from("<HB", data, i + 4)
        frag = data[i + 7:i + 7 + length]
        i += 7 + length
        if rtype == 0 and length == 0:
            i += BLOCK_SIZE - (i % BLOCK_SIZE) if i % BLOCK_SIZE else 0
            continue
        if rtype == 1:
            pending = bytearray()
            try:
                yield from _batch_records(bytes(frag))
            except (ValueError, struct.error):
                pass
        elif rtype == 2:
            pending = bytearray(frag)
        elif rtype == 3:
            pending += frag
        elif rtype == 4:
            pending += frag
            try:
                yield from _batch_records(bytes(pending))
            except (ValueError, struct.error):
                pass
            pending = bytearray()


def _read_block(data: bytes, offset: int, size: int) -> bytes:
    raw = data[offset:offset + size]
    ctype = data[offset + size] if offset + size < len(data) else 0
    if ctype == 1:
        return snappy_decompress(raw)
    if ctype == 0:
        return raw
    raise ValueError(f"unsupported block compression {ctype}")


def _block_entries(block: bytes) -> Iterator[tuple]:
    if len(block) < 4:
        return
    num_restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    end = len(block) - 4 - 4 * num_restarts
    i = 0
    key = b""
    while i < end:
        shared, i = read_varint(block, i)
        non_shared, i = read_varint(block, i)
        vlen, i = read_varint(block, i)
        key = key[:shared] + block[i:i + non_shared]
        i += non_shared
        val = block[i:i + vlen]
        i += vlen
        yield key, val


def read_table(path: Path) -> Iterator[Record]:
    data = Path(path).read_bytes()
    if len(data) < 48 or data[-8:] != b"\x57\xfb\x80\x8b\x24\x75\x47\xdb":
        raise ValueError("not a leveldb table")
    footer = data[-48:]
    _mo, j = read_varint(footer, 0)
    _ms, j = read_varint(footer, j)
    io, j = read_varint(footer, j)
    isz, j = read_varint(footer, j)
    index = _read_block(data, io, isz)
    for _k, handle in _block_entries(index):
        bo, h = read_varint(handle, 0)
        bs, h = read_varint(handle, h)
        try:
            block = _read_block(data, bo, bs)
        except ValueError:
            continue
        for ikey, val in _block_entries(block):
            if len(ikey) < 8:
                continue
            tail = struct.unpack("<Q", ikey[-8:])[0]
            yield Record(ikey[:-8], val, tail >> 8, (tail & 0xFF) == 0)


def read_db(directory: Path, warnings: Optional[list] = None) -> dict:
    """Latest live value per key across all .log and .ldb/.sst files: {key: value}."""
    latest: dict = {}
    d = Path(directory)
    files = sorted(list(d.glob("*.ldb")) + list(d.glob("*.sst"))) + sorted(d.glob("*.log"))
    for f in files:
        try:
            it = read_table(f) if f.suffix in (".ldb", ".sst") else read_log(f)
            for r in it:
                cur = latest.get(r.key)
                if cur is None or r.seq >= cur[0]:
                    latest[r.key] = (r.seq, r.value, r.deleted)
        except Exception as e:  # a damaged file only loses its own records
            if warnings is not None:
                warnings.append(f"{f.name}: {type(e).__name__}: {e}")
    return {k: v for k, (s, v, dead) in latest.items() if not dead}
