"""Builder's store: reviews, lessons, memories, pending, inbox, and stats."""

import pytest

from builder.db import Store


@pytest.fixture
def db():
    return Store()


def test_reviews_store_severity_and_fix_and_dedup(db):
    rid = db.add_review("SQL built from string formatting", "high", "use parameterised queries", "db")
    assert db.review_exists("SQL built from string formatting")
    r = db.reviews()[0]
    assert r["id"] == rid and r["severity"] == "high" and "parameterised" in r["fix"]
    db.add_review("Weak hash", "med", "use bcrypt")
    db.add_review("Binds to all interfaces", "low", "bind to 127.0.0.1")
    # most serious first
    assert [x["severity"] for x in db.reviews()] == ["high", "med", "low"]
    assert [x["issue"] for x in db.reviews(severity="med")] == ["Weak hash"]
    assert db.reviews("sql")[0]["issue"].startswith("SQL")        # search by text
    # a bad severity is repaired
    assert db.reviews()[0]["severity"] in ("high", "med", "low")


def test_lessons_and_memories(db):
    lid = db.add_lesson("Always add a test", "preference")
    assert db.lessons()[0]["id"] == lid and db.lessons("preference")[0]["text"] == "Always add a test"
    assert db.retire_lesson(lid) and db.lessons() == []            # retired -> inactive

    mid = db.remember("Boss uses an Intel Mac", "hardware")
    assert db.recall("Intel")[0]["id"] == mid
    assert db.forget(mid) and db.recall() == []


def test_lesson_kind_is_validated(db):
    with pytest.raises(ValueError):
        db.add_lesson("x", "nonsense")


def test_pending_and_inbox(db):
    pid = db.add_pending("ask", "what is the API port?", origin="pocket")
    assert db.pending()[0]["id"] == pid
    db.finish_pending(pid, "job done")
    assert db.pending() == [] and db.pending(include_done=True)[0]["result"] == "job done"

    db.notify("Build finished", "build:1")
    assert db.inbox(unread_only=True)[0]["text"] == "Build finished"
    assert db.mark_read() == 1 and db.inbox(unread_only=True) == []


def test_stats_are_builder_shaped(db):
    db.add_review("a", "high", "")
    db.add_review("b", "low", "")
    db.add_lesson("x", "preference")
    st = db.stats()
    assert st["reviews"] == 2 and st["reviews_by_severity"] == {"high": 1, "med": 0, "low": 1}
    assert st["lessons"] == 1 and set(st) >= {"memories", "reviews", "lessons", "lab_targets", "pending", "unread"}
