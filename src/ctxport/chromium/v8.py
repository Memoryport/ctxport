"""Deserializer for V8 ValueSerializer output wrapped in Blink's envelope (IndexedDB values)."""
from __future__ import annotations

import struct
from typing import Any

from .leveldb import read_varint


class Undefined:
    def __repr__(self) -> str:
        return "undefined"


UNDEFINED = Undefined()


class V8Error(ValueError):
    pass


class _Reader:
    def __init__(self, buf: bytes, pos: int = 0):
        self.b = buf
        self.i = pos
        self.objs: dict = {}
        self.next_id = 0

    def byte(self) -> int:
        if self.i >= len(self.b):
            raise V8Error("truncated")
        v = self.b[self.i]
        self.i += 1
        return v

    def peek(self) -> int:
        return self.b[self.i] if self.i < len(self.b) else -1

    def varint(self) -> int:
        v, self.i = read_varint(self.b, self.i)
        return v

    def zigzag(self) -> int:
        v = self.varint()
        return (v >> 1) ^ -(v & 1)

    def raw(self, n: int) -> bytes:
        if self.i + n > len(self.b):
            raise V8Error("truncated bytes")
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def double(self) -> float:
        return struct.unpack("<d", self.raw(8))[0]

    def _assign(self, obj: Any) -> int:
        oid = self.next_id
        self.next_id += 1
        self.objs[oid] = obj
        return oid

    def value(self, depth: int = 0) -> Any:
        if depth > 200:
            raise V8Error("too deep")
        while True:
            t = self.byte()
            if t == 0x00:  # padding
                continue
            break
        c = chr(t)
        if c == "_":
            return UNDEFINED
        if c == "0":
            return None
        if c == "T":
            return True
        if c == "F":
            return False
        if c == "I":
            return self.zigzag()
        if c == "U":
            return self.varint()
        if c == "N":
            return self.double()
        if c == "Z":  # BigInt
            bitfield = self.varint()
            n = bitfield >> 1
            digits = self.raw(n)
            v = int.from_bytes(digits, "little")
            return -v if bitfield & 1 else v
        if c == '"':
            n = self.varint()
            return self.raw(n).decode("latin-1")
        if c == "S":
            n = self.varint()
            return self.raw(n).decode("utf-8", "replace")
        if c == "c":
            n = self.varint()
            return self.raw(n).decode("utf-16-le", "replace")
        if c == "^":
            oid = self.varint()
            return self.objs.get(oid)
        if c == "o":
            obj: dict = {}
            self._assign(obj)
            self._props(obj, "{", depth)
            return obj
        if c == "A":  # dense array
            n = self.varint()
            arr: list = []
            self._assign(arr)
            for _ in range(n):
                if self.peek() == ord("-"):
                    self.i += 1
                    arr.append(UNDEFINED)
                else:
                    arr.append(self.value(depth + 1))
            extra: dict = {}
            self._props(extra, "$", depth, trailing=2)
            return arr
        if c == "a":  # sparse array
            n = self.varint()
            sparse: dict = {}
            holder: list = []
            self._assign(holder)
            self._props(sparse, "@", depth, trailing=2)
            arr2 = [UNDEFINED] * min(n, 1_000_000)
            for k, v in sparse.items():
                if isinstance(k, int) and 0 <= k < len(arr2):
                    arr2[k] = v
            holder.extend(arr2)
            return holder
        if c == "D":
            v = self.double()
            self._assign(v)
            return v / 1000.0  # epoch seconds
        if c in ("y", "x"):
            v = c == "y"
            self._assign(v)
            return v
        if c == "n":
            v = self.double()
            self._assign(v)
            return v
        if c == "z":
            bitfield = self.varint()
            self.raw(bitfield >> 1)
            self._assign(0)
            return 0
        if c == "s":
            v = self.value(depth + 1)
            self._assign(v)
            return v
        if c == "R":
            pattern = self.value(depth + 1)
            self.varint()
            self._assign(pattern)
            return pattern
        if c == ";":
            m: dict = {}
            self._assign(m)
            items = []
            while self.peek() != ord(":"):
                items.append(self.value(depth + 1))
            self.i += 1
            self.varint()
            for k in range(0, len(items) - 1, 2):
                key = items[k]
                try:
                    m[key] = items[k + 1]
                except TypeError:
                    m[str(key)] = items[k + 1]
            return m
        if c == "'":
            s: list = []
            self._assign(s)
            while self.peek() != ord(","):
                s.append(self.value(depth + 1))
            self.i += 1
            self.varint()
            return s
        if c == "B":
            n = self.varint()
            v = self.raw(n)
            self._assign(v)
            if self.peek() == ord("V"):
                self.i += 1
                return self._view(v)
            return v
        if c == "~":
            n = self.varint()
            self.varint()
            v = self.raw(n)
            self._assign(v)
            return v
        if c == "V":
            return self._view(b"")
        if c == "r":  # Error
            err: dict = {}
            while True:
                tag = chr(self.byte())
                if tag == ".":
                    break
                if tag in ("m", "s"):
                    err["message" if tag == "m" else "stack"] = self.value(depth + 1)
                elif tag == "c":
                    err["cause"] = self.value(depth + 1)
            self._assign(err)
            return err
        if c == "\\":  # Blink host object
            return self._host()
        raise V8Error(f"unknown tag {t:#x} at {self.i - 1}")

    def _view(self, buf: bytes) -> bytes:
        self.byte()  # subtag
        off = self.varint()
        ln = self.varint()
        if self.peek() != -1 and self.b[self.i] < 0x20:
            pass
        v = buf[off:off + ln]
        self._assign(v)
        return v

    def _props(self, obj: dict, end: str, depth: int, trailing: int = 1) -> None:
        while True:
            if self.peek() == ord(end):
                self.i += 1
                for _ in range(trailing):
                    self.varint()
                return
            k = self.value(depth + 1)
            v = self.value(depth + 1)
            if isinstance(k, float) and k.is_integer():
                k = int(k)
            try:
                obj[k] = v
            except TypeError:
                obj[str(k)] = v

    def _host(self) -> Any:
        tag = chr(self.byte())
        # Blink tags (subset): b Blob, f File, i/e blob index, F file index, I image data, ...
        if tag == "b":
            uuid = self._blink_str()
            mime = self._blink_str()
            size = self.varint()
            v = {"__blob__": True, "uuid": uuid, "type": mime, "size": size}
        elif tag in ("i", "e"):
            v = {"__blob__": True, "index": self.varint()}
        elif tag == "f":
            v = {"__file__": True, "path": self._blink_str(), "name": self._blink_str(),
                 "relative": self._blink_str(), "uuid": self._blink_str(), "type": self._blink_str()}
            # remaining file fields vary by version; stop parsing this value
            raise V8Error("file host object")
        else:
            raise V8Error(f"unsupported host object {tag!r}")
        self._assign(v)
        return v

    def _blink_str(self) -> str:
        n = self.varint()
        return self.raw(n).decode("utf-8", "replace")


def deserialize(buf: bytes, pos: int = 0) -> Any:
    """Blink envelope (0xFF ver [0xFE trailer]) then V8 header (0xFF ver) then one value."""
    r = _Reader(buf, pos)
    if r.peek() == 0xFF:
        r.i += 1
        r.varint()  # blink version
        if r.peek() == 0xFE:  # trailer offset (blink >= 21)
            r.i += 1 + 8 + 4
    if r.peek() == 0xFF:
        r.i += 1
        r.varint()  # v8 version
    return r.value()


def to_plain(v: Any) -> Any:
    """Undefined → None, bytes → None, so results are JSON-friendly."""
    if v is UNDEFINED:
        return None
    if isinstance(v, (bytes, bytearray)):
        return None
    if isinstance(v, dict):
        return {str(k): to_plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_plain(x) for x in v]
    return v
