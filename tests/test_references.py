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
