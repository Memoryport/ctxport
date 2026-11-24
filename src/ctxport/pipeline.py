"""discover → skip unchanged → parse → store (incremental, fault tolerant)."""
from __future__ import annotations

import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .adapters import get_importer, importers
from .adapters.base import Importer
from .store import Store
from .utils import signature


@dataclass
class SyncReport:
    platform: str
    units: int = 0
    skipped: int = 0
    imported: int = 0
    conversations: int = 0
    messages: int = 0
    memories_new: int = 0
    warnings: list = field(default_factory=list)


def import_unit(store: Store, imp: Importer, unit: Path, force: bool, report: SyncReport) -> None:
    key = imp.unit_key(unit)
    sig = signature(unit)
    report.units += 1
    if not force and store.source_signature(key) == sig:
        report.skipped += 1
        return
    try:
        res = imp.load(unit)
    except Exception as e:  # one bad file never stops a sync
        msg = f"{unit}: {type(e).__name__}: {e}"
        report.warnings.append(msg)
        store.set_source(key, imp.name, str(unit), "error:" + sig, 0, 1)
        if os.environ.get("CTXPORT_DEBUG"):
            traceback.print_exc()
        return
    for conv in res.conversations:
        if not conv.messages and not conv.branches:
            continue
        if not conv.source_path:
            conv.source_path = str(unit)
        store.upsert_conversation(conv, key)
        report.conversations += 1
        report.messages += len(conv.messages)
    for mem in res.memories:
        if store.upsert_memory(mem) == "new":
            report.memories_new += 1
    store.commit()
    report.imported += 1
    report.warnings += [f"{Path(unit).name}: {w}" for w in res.warnings]
    store.set_source(key, imp.name, str(unit), sig, len(res.conversations), len(res.warnings))


def import_path(store: Store, platform: str, path: Optional[Path], force: bool = False,
                progress: Optional[Callable[[str], None]] = None) -> SyncReport:
    imp = get_importer(platform)
    report = SyncReport(platform)
    roots = [Path(path).expanduser()] if path else [r for r in imp.default_roots() if Path(r).exists()]
    if path and not Path(path).expanduser().exists():
        report.warnings.append(f"path not found: {path}")
        return report
    for root in roots:
        try:
            units = imp.discover(root)
        except Exception as e:
            report.warnings.append(f"{root}: discover failed: {e}")
            continue
        for unit in units:
            import_unit(store, imp, unit, force, report)
            if progress and report.units % 50 == 0:
                progress(f"{platform}: {report.units} units…")
    return report


def sync(store: Store, only: Optional[list] = None, force: bool = False,
         progress: Optional[Callable[[str], None]] = None) -> list:
    reports = []
    for imp in importers():
        if only and imp.name not in only:
            continue
        roots = [r for r in imp.default_roots() if Path(r).exists()]
        if not roots:
            continue
        reports.append(import_path(store, imp.name, None, force, progress))
    return reports
