import pytest


def test_memory_recall_by_keyword(db):
    db.remember("Boss is interested in sneaker reselling", topic="interests")
    db.remember("Prefers sources from the last two years")
    hits = db.recall("what is he into for reselling")
    assert hits and "sneaker" in hits[0]["content"]
    assert len(db.recall("")) == 2


def test_recall_tolerates_fts_syntax(db):
    db.remember("AAPL breakout setup")
    assert db.recall('AAPL" OR (* NEAR') != []


def test_sources_are_unique_per_url_and_keep_longest_text(db):
    a = db.add_source("https://x.org/a", "A", "x.org", text="short")
    b = db.add_source("https://x.org/a", "", "x.org", text="a much longer full text of the page")
    assert a == b
    s = db.get_source(a)
    assert s["title"] == "A" and s["text"].startswith("a much longer")
    assert db.sources("longer")[0]["id"] == a  # FTS sees the updated text


def test_findings_history_and_search(db):
    fid = db.add_finding("Sneaker resale market is ~$10B", "reported", 0.6, "sneakers", [3, 1, 3])
    assert db.get_finding(fid)["source_ids"] == [1, 3]
    f = db.update_finding(fid, "verified", 0.9, [7], "second independent source agrees")
    assert f["status"] == "verified" and f["source_ids"] == [1, 3, 7] and f["confidence"] == 0.9
    hist = db.finding_history(fid)
    assert [h["status"] for h in hist] == ["reported", "verified"]
    assert hist[-1]["reason"] == "second independent source agrees"
    assert db.findings("resale")[0]["id"] == fid
    assert db.findings(status="refuted") == []
    assert db.update_finding(999, reason="x") is None


def test_finding_status_is_validated(db):
    with pytest.raises(ValueError):
        db.add_finding("x", "probably")


def test_reports_and_stats(db):
    rid = db.add_report("q?", "# body", "plan", {"k": 1})
    assert db.get_report(rid)["data"] == {"k": 1}
    assert db.reports()[0]["question"] == "q?"
    db.add_finding("c", "assumption")
    st = db.stats()
    assert st["reports"] == 1 and st["findings_by_status"]["assumption"] == 1
