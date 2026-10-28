"""Builder's reference library: ingest a document, recall relevant passages, inject into build prompts."""
from pathlib import Path

import pytest

from builder import references
from builder.buildagent import make_propose
from builder.db import Store
from builder.memory import Memory


def _mem():
    return Memory(Store(":memory:"))


def test_chunking_splits_long_text():
    text = "\n\n".join(f"Paragraph {i} about coding patterns and games." for i in range(200))
    chunks = references.chunk_text(text, size=300)
    assert len(chunks) > 1
    assert all(len(c) <= 700 for c in chunks)       # roughly bounded


def test_ingest_and_recall_relevant_passages(tmp_path):
    mem = _mem()
    doc = tmp_path / "book.md"
    doc.write_text("Pong uses two paddles and a bouncing ball.\n\n"
                   "Space invaders has rows of aliens that march and a ship that shoots bullets.\n\n"
                   "Breakout has a paddle and bricks to clear.")
    info = mem.learn_doc(str(doc))
    assert info["chunks"] >= 1
    assert mem.documents()[0]["source"] == "book.md"

    note = mem.reference_note("build a space invaders game")
    assert "aliens" in note.lower() or "invaders" in note.lower()
    # an unrelated query shouldn't pull the invaders passage to the top if nothing matches
    assert "reference library" in note.lower()


def test_reference_passages_reach_the_build_prompt(tmp_path):
    mem = _mem()
    doc = tmp_path / "notes.txt"
    doc.write_text("Pong tip: make the ball speed up slightly on every paddle hit for tension.")
    mem.learn_doc(str(doc))

    note = mem.reference_note("a pong game")
    seen = []
    propose = make_propose(lambda p: (seen.append(p), "x\nFILES_JSON: {}")[1], "a pong game",
                           log=lambda m: None, memory_note=note)
    propose({"attempt": 1, "failure": None, "diff": ""})
    assert "speed up slightly" in seen[0]            # his uploaded note informed the build prompt


def test_unreadable_type_gives_a_clear_error(tmp_path):
    mem = _mem()
    bad = tmp_path / "image.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n not really")
    with pytest.raises(references.ReferenceError):
        mem.learn_doc(str(bad))


def test_forget_removes_a_document(tmp_path):
    mem = _mem()
    doc = tmp_path / "x.md"
    doc.write_text("some coding reference text about functions and loops")
    mem.learn_doc(str(doc))
    assert mem.forget_doc("x.md") >= 1
    assert mem.documents() == []


def test_ingest_many_walks_a_folder_and_skips_junk(tmp_path):
    mem = _mem()
    (tmp_path / "ch1.md").write_text("Game loop: update then draw each frame.")
    (tmp_path / "ch2.md").write_text("Object pool pattern reuses objects.")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("should be skipped")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG not text")

    summary = mem.learn_docs([str(tmp_path)])
    assert summary["docs"] == 2          # two .md files only
    assert summary["errors"] == []
    assert {d["source"] for d in mem.documents()} == {"ch1.md", "ch2.md"}


def test_is_url_detection():
    assert references.is_url("https://example.com/book.pdf")
    assert references.is_url("http://example.com/x")
    assert not references.is_url("/home/me/book.pdf")
    assert not references.is_url("book.md")


def test_learn_insight_is_stored_and_recallable():
    mem = _mem()
    mem.learn_insight("pong game", "Use requestAnimationFrame and a fixed timestep for smooth motion.",
                      source="build")
    docs = mem.documents()
    assert any(d["source"] == "learned-insights" for d in docs)
    note = mem.reference_note("build a pong game")
    assert "requestAnimationFrame" in note or "fixed timestep" in note


def test_build_with_a_stronger_model_distils_an_insight(tmp_path):
    # When a stronger model is available and plans a working build, the approach is learned for next time.
    import json as _json
    from builder.buildagent import run_build
    from builder.db import Store
    from builder.memory import Memory
    from builder.projects import Project

    class Cfg:
        code_dirs = [str(tmp_path)]

    store = Store(":memory:")
    mem = Memory(store)
    proj = Project(tmp_path / "site", Cfg())
    page = ('<!doctype html><html><head><title>Joe\'s Tacos</title>'
            '<meta name="viewport" content="width=device-width"></head><body><h1>Joe\'s Tacos</h1></body></html>')
    test_py = "def test_ok():\n    assert 'tacos' in open('index.html').read().lower()\n"
    files = {"index.html": page, "test_site.py": test_py}

    class Model:
        def __init__(self, tag): self.tag = tag
        def available(self): return True
        def chat(self, msgs):
            p = msgs[-1]["content"]
            if "BEFORE writing any code" in p:
                return {"content": "- taco hero\n- menu\n- hours"}      # the plan (from the stronger model)
            if "review your own work" in p:
                return {"content": "better\nFILES_JSON: " + _json.dumps(files)}
            return {"content": "draft\nFILES_JSON: " + _json.dumps(files)}

    run_build("a taco truck landing page", proj, Model("fast"), memory=mem, tries=1,
              log=lambda m: None, fallback_llm=Model("strong"))
    assert any(d["source"] == "learned-insights" for d in mem.documents())   # it learned from the strong model


def test_assistant_uses_references_and_gates_offense_by_lab_mode(tmp_path):
    from builder.assistant import Assistant
    from builder.config import Config
    from builder.db import Store
    from builder.memory import Memory

    class FakeLLM:
        model = "x"
        def available(self): return True
        def chat(self, *a, **k): return {"content": "ok"}

    cfg = Config(home=tmp_path)
    store = Store(":memory:")
    mem = Memory(store, None, cfg)
    mem.learn_insight("sql injection", "Use parameterised queries; never build SQL from input.", source="book")
    a = Assistant(FakeLLM(), cfg, mem, store)

    # references feed the chat; offense note is OFF by default
    sp = a.system_prompt("how do I prevent sql injection?")
    assert "parameterised queries" in sp
    assert "Authorized-lab mode is ON" not in sp

    # turning on lab mode unlocks the (bounded) offensive-teaching note
    store.authorize_lab_target("my-practice-vm", "vm", "owned")
    sp2 = a.system_prompt("walk me through exploiting this")
    assert "Authorized-lab mode is ON" in sp2
    assert "never systems the boss doesn't own" in sp2
