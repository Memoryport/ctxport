from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pack_module():
    spec = importlib.util.spec_from_file_location("pack_extension", ROOT / "scripts" / "pack_extension.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_extension_zip_has_only_runtime_files(tmp_path):
    mod = _pack_module()
    out = mod.pack(ROOT / "extension", tmp_path / "ext.zip")
    with zipfile.ZipFile(out) as z:
        names = [n.split("/", 1)[1] for n in z.namelist()]
    assert "manifest.json" in names and "core.js" in names and "popup.js" in names
    assert not any(n.startswith("test/") or n.endswith(".md") for n in names)
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["icons"].values()) <= set(names)
    assert out.stat().st_size < 100 * 1024


def test_extension_zip_is_reproducible(tmp_path):
    mod = _pack_module()
    a = mod.pack(ROOT / "extension", tmp_path / "a.zip").read_bytes()
    b = mod.pack(ROOT / "extension", tmp_path / "b.zip").read_bytes()
    assert a == b
