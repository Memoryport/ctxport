import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.ctxport, ~/.claude, ~/.codex, ~/.gemini during tests."""
    monkeypatch.setenv("CTXPORT_HOME", str(tmp_path / "ctxport_home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no_claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no_codex"))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "no_gemini"))
    monkeypatch.setenv("CHERRY_STUDIO_HOME", str(tmp_path / "no_cherry"))
    monkeypatch.setenv("CHATBOX_HOME", str(tmp_path / "no_chatbox"))
    monkeypatch.setenv("CTXPORT_DOWNLOADS", str(tmp_path / "no_downloads"))
    monkeypatch.delenv("CTXPORT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("CTXPORT_STORE_RAW", raising=False)
    yield
