"""Importer / Exporter base classes and the adapter registry.

Adding a platform = one new module in this package with a subclass decorated by @register.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..models import ImportResult, MemoryItem, OutputFile

IMPORTERS: dict = {}
EXPORTERS: dict = {}


def register(cls):
    """Class decorator: registers an Importer or Exporter subclass by its `name`."""
    if issubclass(cls, Importer):
        IMPORTERS[cls.name] = cls
    if issubclass(cls, Exporter):
        EXPORTERS[cls.name] = cls
    return cls


def inbox_units(root: Path) -> list:
    """Files the browser extension dropped into ~/.ctxport/inbox/<platform>/ (one unit per conversation)."""
    root = Path(root)
    if root.is_file():
        return [root]
    files = {}
    for p in list(root.glob("*.json")) + list(root.glob("*.json.gz")):
        if p.name in ("_index.json", "_index.json.gz"):
            continue
        stem = p.name[:-3] if p.name.endswith(".gz") else p.name
        if stem not in files or p.name.endswith(".gz"):  # prefer the compressed copy
            files[stem] = p
    return sorted(files.values())


_ACCOUNT_FILE = re.compile(r"^_account(\.[A-Za-z0-9._-]+)?\.json$")


def is_account_file(path) -> bool:
    """`_account.json`, or `_account.<account id>.json` when one platform login has several accounts
    (e.g. Claude.ai organisations): memories, custom instructions and projects, not a conversation."""
    return bool(_ACCOUNT_FILE.match(Path(path).name))


def downloads_dir() -> Path:
    """Where browsers save official export zips (override with CTXPORT_DOWNLOADS; "off" disables the scan)."""
    import os
    v = os.environ.get("CTXPORT_DOWNLOADS")
    if v:
        return Path(v).expanduser()
    return Path.home() / "Downloads"


def export_zips(root: Path, looks_like) -> list:
    """Zip files directly in `root` whose file list matches an official export (only the zip directory is read)."""
    import zipfile
    root = Path(root)
    if str(root) == "off" or not root.is_dir():
        return []
    out = []
    for p in sorted(root.glob("*.zip")):
        try:
            with zipfile.ZipFile(p) as z:
                names = {n.rsplit("/", 1)[-1] for n in z.namelist()}
        except (zipfile.BadZipFile, OSError):
            continue
        if looks_like(names):
            out.append(p)
    return out


def is_inbox(path: Path) -> bool:
    return Path(path).parent.parent.name == "inbox" or Path(path).parent.name == "inbox"


class Importer:
    name: str = ""
    label: str = ""
    how_to_get: str = ""
    experimental: bool = False

    def default_roots(self) -> list:
        """Auto-detected local locations. Export-file platforms return []."""
        return []

    def discover(self, root: Path) -> list:
        """Split a root into independently re-importable units (e.g. one per session file)."""
        return [Path(root)]

    def load(self, unit: Path) -> ImportResult:
        raise NotImplementedError

    def unit_key(self, unit: Path) -> str:
        return f"{self.name}:{Path(unit).expanduser().resolve()}"


@dataclass
class ExportOptions:
    out_dir: Path = Path("ctxport-out")
    apply: bool = False
    project: Optional[Path] = None
    platform: Optional[str] = None
    limit: Optional[int] = None
    since: Optional[float] = None
    include_candidates: bool = False
    include_tools: bool = False
    include_reasoning: bool = False
    redact: bool = True
    include_raw: bool = False
    extra: dict = field(default_factory=dict)


class Exporter:
    name: str = ""
    label: str = ""
    instructions: str = ""
    # True when the output can be written into a real location with --apply
    appliable: bool = False

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        raise NotImplementedError


__all__ = ["Importer", "Exporter", "ExportOptions", "register", "IMPORTERS", "EXPORTERS",
           "ImportResult", "MemoryItem", "OutputFile"]
