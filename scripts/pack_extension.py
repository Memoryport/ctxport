"""Pack the browser extension into a small, reproducible zip for the Chrome / Edge "Load unpacked" flow.

    python scripts/pack_extension.py            # → dist/ctxport-extension-<version>.zip

Only the files the browser needs are included (manifest, scripts, popup, icons). Tests, README and anything
else in extension/ stay out. Entries get a fixed timestamp so the same sources always give the same zip.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def runtime_files(ext_dir: Path) -> list:
    """Every file the manifest references, plus the popup's own scripts. Fails if one is missing."""
    manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    names = {"manifest.json"}
    bg = manifest.get("background", {}).get("service_worker")
    if bg:
        names.add(bg)
    for cs in manifest.get("content_scripts", []):
        names.update(cs.get("js", []))
        names.update(cs.get("css", []))
    action = manifest.get("action", {})
    if action.get("default_popup"):
        names.add(action["default_popup"])
    names.update(action.get("default_icon", {}).values())
    names.update(manifest.get("icons", {}).values())
    # scripts and styles the popup page loads itself
    for page in [n for n in list(names) if n.endswith(".html")]:
        html = (ext_dir / page).read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href)="([^":]+?\.(?:js|css|png|svg))"', html):
            names.add(str((Path(page).parent / ref).as_posix()).lstrip("./"))
    # workers pull shared code in with importScripts("…")
    for js in [n for n in list(names) if n.endswith(".js")]:
        if (ext_dir / js).is_file():
            names.update(re.findall(r"""importScripts\(\s*["']([^"']+)["']""", (ext_dir / js).read_text(encoding="utf-8")))
    missing = [n for n in sorted(names) if not (ext_dir / n).is_file()]
    if missing:
        raise SystemExit(f"extension files referenced but missing: {', '.join(missing)}")
    return sorted(names)


def pack(ext_dir: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in runtime_files(ext_dir):
            info = zipfile.ZipInfo(f"ctxport-extension/{name}", date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, (ext_dir / name).read_bytes(), compresslevel=9)
    return out


def main(argv=None) -> int:
    version = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))["version"]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist" / f"ctxport-extension-{version}.zip"))
    args = ap.parse_args(argv)
    out = pack(EXT, Path(args.out))
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    print(f"{out} ({out.stat().st_size / 1024:.1f} KB, {len(names)} files)")
    for n in names:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
