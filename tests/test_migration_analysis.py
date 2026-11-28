import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "migration"))
import analyze as A  # noqa: E402


def _row(p, q, correct, in_ctx, in_inf, method="m"):
    return {"persona": p, "qid": q, "correct": correct, "in_ctx": in_ctx, "in_inf": in_inf, "method": method,
            "reader": "r", "type": "single_fact", "split": "test", "abstained": False, "trap": False,
            "ctx_chars": 10}


def test_losses_partition_errors_exactly_even_with_lucky_guesses():
    rows = [_row("a", 1, True, True, True), _row("a", 2, False, True, True), _row("a", 3, True, False, True),
            _row("a", 4, False, False, True), _row("a", 5, True, False, False), _row("a", 6, False, False, False)]
    d = A.decompose(rows)
    assert abs(d["l_ext"] + d["l_bud"] + d["l_read"] - (1 - d["acc"])) < 1e-12
    assert abs(d["gamma"] - 2 / 6) < 1e-12
    # the naive three-term sum overshoots the error rate by exactly gamma
    naive = (1 - d["rho"]) + d["rho"] * (1 - d["contain"] / d["rho"]) + d["contain"] * (1 - d["upsilon"])
    assert abs(naive - (1 - d["acc"] + d["gamma"])) < 1e-12


def test_nesting_violation_counted_and_folded_into_E():
    rows = [_row("a", 1, True, True, False)]
    d = A.decompose(rows)
    assert d["nesting_violations"] == 1 and d["rho"] == 1.0


def test_holm_is_monotone_and_capped():
    assert [round(x, 9) for x in A.holm([0.01, 0.04, 0.03])] == [0.03, 0.06, 0.06]
    assert A.holm([0.9, 0.8]) == [1.0, 1.0]


def test_paired_exact_sign_flip_minimum():
    a = [_row(p, q, True, True, True, "A") for p in "abcdef" for q in range(4)]
    b = [_row(p, q, False, True, True, "B") for p in "abcdef" for q in range(4)]
    s = A.paired(a, b, n_boot=200)
    assert s["delta"] == 100 and s["wins"] == 6 and abs(s["p"] - 2 / 64) < 1e-12
