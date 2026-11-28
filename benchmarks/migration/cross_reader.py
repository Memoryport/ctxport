"""Compare methods across readers on the same personas/questions (test split)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as A
M = ["none", "recent@1400", "bm25@1400", "rolling@1400", "summary@1400", "chmd-det@1400", "chmd-detT@1400",
     "chmd-llm@1400", "chmd-write@1400"]
PAIRS = [("chmd-write@1400", "summary@1400"), ("chmd-write@1400", "rolling@1400"), ("summary@1400", "rolling@1400"),
         ("chmd-write@1400", "chmd-llm@1400")]
rows = A.load(sys.argv[1:], "test")
readers = sorted({r["reader"] for r in rows})
print("| method | " + " | ".join(readers) + " |\n|---|" + "---|" * len(readers))
for m in M:
    cells = []
    for rd in readers:
        rs = [r for r in rows if r["reader"] == rd and r["method"] == m]
        if rs:
            lo, hi = A.boot_ci(A.by_persona(rs), n_boot=2000)
            cells.append(f"{100*sum(r['correct'] for r in rs)/len(rs):.1f} [{lo:.0f},{hi:.0f}]")
        else:
            cells.append("–")
    print(f"| {m} | " + " | ".join(cells) + " |")
print()
for a, b in PAIRS:
    out = []
    for rd in readers:
        ra = [r for r in rows if r["reader"] == rd and r["method"] == a]
        rb = [r for r in rows if r["reader"] == rd and r["method"] == b]
        s = A.paired(ra, rb, n_boot=2000) if ra and rb else None
        out.append(f"{rd}: {s['delta']:+.1f} [{s['ci95'][0]:+.1f},{s['ci95'][1]:+.1f}] p={s['p']:.3g} ({s['wins']}/{s['losses']})" if s else f"{rd}: –")
    print(f"{a} − {b}:  " + ";  ".join(out))
