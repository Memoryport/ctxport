"""Exit codes and error messages people and scripts rely on, and a few import/export details."""
import json

import fixtures as fx
import pytest

from ctxport.adapters import get_importer
from ctxport.cli import main
from ctxport.models import MemoryItem
from ctxport.store import Store, default_home


@pytest.mark.parametrize("name,content", [("empty.json", "{}"), ("bad.json", "{broken"), ("junk.zip", "not a zip"),
                                          ("list.json", "[]")])
def test_import_that_reads_nothing_fails(tmp_path, capsys, name, content):
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    assert main(["import", "chatgpt", str(f)]) == 1
    err = capsys.readouterr().err
    assert "nothing was imported" in err and "ChatGPT" in err
    assert main(["import", "chatgpt", str(tmp_path / "missing.json")]) == 1


def test_import_that_reads_something_succeeds(tmp_path):
    assert main(["import", "chatgpt", str(fx.chatgpt_export(tmp_path))]) == 0
    assert main(["import", "chatgpt", str(fx.chatgpt_export(tmp_path))]) == 0     # unchanged on the second run


def test_migrate_stops_when_nothing_was_imported(tmp_path):
    out = tmp_path / "out"
    assert main(["migrate", "chatgpt", str(tmp_path / "missing.json"), "claude-code", "--out", str(out)]) == 1
    assert not out.exists()


def test_llm_unreachable_is_one_clean_line(tmp_path, capsys):
    main(["import", "chatgpt", str(fx.chatgpt_export(tmp_path))])
    capsys.readouterr()
    args = ["--llm", "--base-url", "http://127.0.0.1:9/v1", "--model", "m"]
    assert main(["extract", *args]) == 2
    err = capsys.readouterr().err
    assert "cannot reach LLM" in err and "Traceback" not in err
    assert main(["profile", *args]) == 2
    assert "cannot reach LLM" in capsys.readouterr().err


def test_handoff_llm_unreachable_fails_cleanly(tmp_path, capsys):
    s = Store()
    from ctxport.models import Conversation, Message
    c = Conversation(platform="test", native_id="long", title="t")
    c.messages = [Message(role="user" if i % 2 == 0 else "assistant", text=f"m{i}") for i in range(40)]
    s.upsert_conversation(c)
    s.commit()
    s.close()
    assert main(["handoff", "test:long", "--llm", "--base-url", "http://127.0.0.1:9/v1", "--model", "m"]) == 2
    assert "cannot reach LLM" in capsys.readouterr().err


def test_damaged_database_is_explained(tmp_path, capsys):
    db = default_home() / "ctxport.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not sqlite " * 100)
    assert main(["stats"]) == 2
    err = capsys.readouterr().err
    assert "can't be read" in err and "Traceback" not in err


def test_generic_import_without_times_keeps_them_unknown(tmp_path):
    f = tmp_path / "chat.json"
    f.write_text(json.dumps([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]),
                 encoding="utf-8")
    imp = get_importer("openai-messages")
    (conv,) = imp.load(f).conversations
    assert conv.created_at is None and conv.updated_at is None


def test_antigravity_accepts_a_parent_folder(tmp_path):
    g = fx.antigravity_home(tmp_path)
    imp = get_importer("antigravity")
    assert imp.discover(g)                                    # ~/.gemini itself
    assert imp.discover(g.parent / g.name) == imp.discover(g)
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    import shutil
    shutil.copytree(g / "antigravity", home / ".gemini" / "antigravity")
    assert imp.discover(home)                                 # the home folder, like other platforms


def test_exports_do_not_reveal_local_paths(tmp_path):
    from ctxport.adapters._render import project_name, render_memories_plain
    assert project_name("project:/home/someone/workspace/acme-api") == "acme-api"
    assert project_name("project:C:\\Users\\someone\\code\\tool\\") == "tool"
    text = render_memories_plain([MemoryItem(text="Uses Django", scope="project:/home/someone/workspace/acme-api",
                                             status="accepted")])
    assert "acme-api" in text and "/home/someone" not in text


def test_help_and_small_budget_messages(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["memories", "--help"])
    out = capsys.readouterr().out
    assert "~80%" in out and "option_strings" not in out
    s = Store()
    s.upsert_memory(MemoryItem(text="I am a backend engineer", status="accepted", category="identity"))
    s.commit()
    s.close()
    assert main(["profile", "--max-chars", "20"]) == 1
    assert "too small" in capsys.readouterr().err


def test_mcp_remember_answers_every_outcome(tmp_path):
    from ctxport import mcp_server
    first = mcp_server.remember("The user studies statistics at a university in Canada")
    again = mcp_server.remember("The user studies statistics at a university in Canada!")
    assert "Saved" in first and "Already known" in again
    s = Store()
    assert len(s.memories(status=None)) == 1
    s.close()

