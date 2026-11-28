import time
from pathlib import Path

from ctxport import distill as D


def _fact(i, year, count=1, cat="identity"):
    ts = time.mktime((year, 6, 1, 0, 0, 0, 0, 0, -1))
    return {"id": f"c{i}", "text": f"fact number {i} from {year}", "category": cat, "first": ts, "last": ts,
            "count": count, "sources": [f"s{i}"], "covers": [f"f{i}"]}


def _canon():
    # few early facts, many recent ones: an importance cut alone would keep only the recent year
    return ([_fact(i, 2023) for i in range(5)] + [_fact(100 + i, 2024) for i in range(5)]
            + [_fact(200 + i, 2026, count=5) for i in range(200)])


def test_every_year_gets_a_share_and_shares_fit_the_budget():
    shares = D.year_shares(D.facts_by_year(_canon()), 1400)
    assert set(shares) == {"2023", "2024", "2026"}
    assert min(shares.values()) >= int(1400 * 0.08) - 1
    assert sum(shares.values()) <= 1400


def test_render_years_keeps_old_years_within_budget():
    text, cov = D.render_years(_canon(), 1400)
    assert len(text) <= 1400
    assert "## 2023" in text and "2023" in text.split("## 2024")[0]
    assert "from 2023" in text and "from 2026" in text
    assert 0 < cov < 1


def test_write_years_uses_the_model_per_year_and_falls_back_when_a_call_fails():
    calls = []

    def chat(system, user):
        calls.append(system)
        if "2024" in system:
            raise RuntimeError("quota")
        return "- written bullet\n"

    text, written = D.write_years(_canon(), 4000, chat)
    assert len(calls) == 3 and written == 2
    assert text.count("written bullet") == 2
    assert "from 2024" in text          # the failed year is rendered without the model, not dropped
    assert len(text) <= 4000


def test_write_years_trims_an_overlong_reply_to_whole_lines():
    text, _ = D.write_years(_canon()[:5], 300, lambda s, u: "- " + "x" * 50 + "\n" + "- " + "y" * 500)
    assert len(text) <= 300 and "y" not in text


def test_codex_gets_a_private_home_with_only_the_login(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    (real / "auth.json").write_text("{}")
    (real / "AGENTS.md").write_text("profile of the user")
    monkeypatch.setenv("CODEX_HOME", str(real))
    env = D.agent_env("codex", tmp_path / "t")
    home = Path(env["CODEX_HOME"])
    assert home != real and (home / "auth.json").is_symlink() and not (home / "AGENTS.md").exists()
    assert D.agent_env("claude-code", tmp_path / "t2") is None
