"""Adapter registry. Every module listed here registers its Importer/Exporter classes."""
from __future__ import annotations

import importlib

from .base import EXPORTERS, IMPORTERS, Exporter, Importer

_MODULES = [
    "chatgpt", "claude_ai", "claude_code", "codex", "gemini_cli", "gemini_web",
    "antigravity", "cherry_studio", "chatbox", "generic",
]
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    for m in _MODULES:
        importlib.import_module(f"{__name__}.{m}")
    _loaded = True


def importers() -> list:
    _load()
    return [cls() for cls in IMPORTERS.values()]


def exporters() -> list:
    _load()
    return [cls() for cls in EXPORTERS.values()]


def get_importer(name: str) -> Importer:
    _load()
    if name not in IMPORTERS:
        raise KeyError(f"unknown import platform '{name}'. Known: {', '.join(sorted(IMPORTERS))}")
    return IMPORTERS[name]()


def get_exporter(name: str) -> Exporter:
    _load()
    if name not in EXPORTERS:
        raise KeyError(f"unknown export target '{name}'. Known: {', '.join(sorted(EXPORTERS))}")
    return EXPORTERS[name]()
