"""SQLite storage: Scholar's research library.

One file (``~/.scholar/scholar.db``) holds everything — memory, every source
he has read, every finding with its evidence and history, and his reports.
Moving him to the Pi is just copying that file. Standard library only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'note',    -- note | preference | fact
    topic       TEXT NOT NULL DEFAULT 'general',
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, topic, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, topic) VALUES (new.id, new.content, new.topic);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, topic) VALUES ('delete', old.id, old.content, old.topic);
END;

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,
    title       TEXT,
    domain      TEXT,
    kind        TEXT NOT NULL DEFAULT 'web',     -- web | paper | reference | onion
    tier        INTEGER NOT NULL DEFAULT 3,      -- 1 primary/peer-reviewed ... 4 unvetted
    published   TEXT,
    text        TEXT,                            -- readable text, truncated
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
    title, text, content='sources', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS sources_ai AFTER INSERT ON sources BEGIN
    INSERT INTO sources_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS sources_au AFTER UPDATE ON sources BEGIN
    INSERT INTO sources_fts(sources_fts, rowid, title, text) VALUES ('delete', old.id, old.title, old.text);
    INSERT INTO sources_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY,
    claim       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'assumption',
    confidence  REAL NOT NULL DEFAULT 0.5,       -- 0..1
    topic       TEXT NOT NULL DEFAULT 'general',
    source_ids  TEXT NOT NULL DEFAULT '[]',      -- JSON list of sources.id
    notes       TEXT NOT NULL DEFAULT '',
    severity    TEXT,                            -- security reviews: high | med | low (NULL for research findings)
    fix         TEXT,                            -- security reviews: the recommended fix (NULL for research findings)
    investigation_id INTEGER,
    checks      TEXT NOT NULL DEFAULT '[]',      -- JSON list of skeptic flags (outdated, single-source, ...)
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS findings_fts USING fts5(
    claim, topic, notes, content='findings', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS findings_ai AFTER INSERT ON findings BEGIN
    INSERT INTO findings_fts(rowid, claim, topic, notes) VALUES (new.id, new.claim, new.topic, new.notes);
END;
CREATE TRIGGER IF NOT EXISTS findings_au AFTER UPDATE ON findings BEGIN
    INSERT INTO findings_fts(findings_fts, rowid, claim, topic, notes)
        VALUES ('delete', old.id, old.claim, old.topic, old.notes);
    INSERT INTO findings_fts(rowid, claim, topic, notes) VALUES (new.id, new.claim, new.topic, new.notes);
END;

-- Every change of a conclusion is kept, so Scholar can say *why* he changed his mind.
CREATE TABLE IF NOT EXISTS finding_history (
    id          INTEGER PRIMARY KEY,
    finding_id  INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    confidence  REAL NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    changed_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Layer 3: evidence for and against each finding, with the exact quote.
CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY,
    finding_id  INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL,
    stance      TEXT NOT NULL DEFAULT 'supports', -- supports | contradicts
    quote       TEXT NOT NULL DEFAULT '',
    quote_ok    INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    UNIQUE(finding_id, source_id, stance, quote)
);

-- Layer 3: the research graph. Company -> subsidiary -> supplier -> market ...
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type        TEXT NOT NULL DEFAULT 'thing',  -- company | person | product | market | place | org | study | thing
    description TEXT NOT NULL DEFAULT '',
    explored    INTEGER NOT NULL DEFAULT 0,     -- has a multi-hop search been run on it?
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY,
    subject_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate   TEXT NOT NULL,                  -- e.g. subsidiary_of, supplies, competes_with, sells_in
    object_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    source_id   INTEGER,
    date        TEXT NOT NULL DEFAULT '',       -- when the relation was true/reported, per the source
    investigation_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(subject_id, predicate, object_id, source_id)
);
CREATE TABLE IF NOT EXISTS mentions (
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL,
    investigation_id INTEGER,
    PRIMARY KEY (entity_id, source_id)
);

-- Investigations and their question trees (recursive planning).
CREATE TABLE IF NOT EXISTS investigations (
    id          INTEGER PRIMARY KEY,
    question    TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'research',
    depth       TEXT NOT NULL DEFAULT 'standard',
    status      TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
    summary     TEXT NOT NULL DEFAULT '',
    report_id   INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES questions(id),
    text        TEXT NOT NULL,
    dimension   TEXT NOT NULL DEFAULT '',       -- demand, competitors, costs, ... (business research)
    level       INTEGER NOT NULL DEFAULT 1,     -- 1 = from the plan, 2+ = discovered along the way
    origin      TEXT NOT NULL DEFAULT 'plan',   -- plan | gap | hop | skeptic
    status      TEXT NOT NULL DEFAULT 'open',   -- open | answered | unanswered
    answer      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- Which searches actually produced evidence — Scholar reuses the good ones.
CREATE TABLE IF NOT EXISTS queries (
    id          INTEGER PRIMARY KEY,
    investigation_id INTEGER,
    question_id INTEGER,
    text        TEXT NOT NULL,
    backend     TEXT NOT NULL,                  -- web | papers | wiki | filings | library | citations
    hits        INTEGER NOT NULL DEFAULT 0,
    facts       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Tables pulled out of documents, saved as CSV for the sandbox.
CREATE TABLE IF NOT EXISTS doc_tables (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    caption     TEXT NOT NULL DEFAULT '',
    n_rows      INTEGER NOT NULL,
    n_cols      INTEGER NOT NULL,
    csv_path    TEXT NOT NULL,
    preview     TEXT NOT NULL DEFAULT '',
    UNIQUE(source_id, idx)
);

CREATE TABLE IF NOT EXISTS glossary (
    id          INTEGER PRIMARY KEY,
    term        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    definition  TEXT NOT NULL,
    domain      TEXT NOT NULL DEFAULT 'general',
    source_id   INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Layer 4: learning from experience. Only the boss's feedback (origin 'user') and outcomes
-- verified by Scholar's own checks (origin 'verified') become lessons — never a random web page.
CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                  -- preference | correction | strategy | source_note
    text        TEXT NOT NULL,
    topic       TEXT NOT NULL DEFAULT 'general',
    origin      TEXT NOT NULL DEFAULT 'user',   -- user | verified
    ref         TEXT NOT NULL DEFAULT '',       -- e.g. report:3, finding:12
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Work waiting for the internet: investigations answered offline that need a recheck, and things a
-- device (ESP32 terminal, Pi) was asked while it couldn't reach Scholar.
CREATE TABLE IF NOT EXISTS pending (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                  -- research | plan | ask
    text        TEXT NOT NULL,
    depth       TEXT NOT NULL DEFAULT 'quick',
    origin      TEXT NOT NULL DEFAULT '',       -- offline-recheck | device name
    ref         TEXT NOT NULL DEFAULT '',       -- e.g. investigation:3
    asked_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    done_at     TEXT,
    result      TEXT NOT NULL DEFAULT ''        -- e.g. job:4 / report:7 / the answer
);

-- Messages for the boss (answers to queued questions, finished research), shown on the dashboard.
CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,
    ref         TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    read        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY,
    question    TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'research', -- research | plan
    body        TEXT NOT NULL,                    -- markdown
    data        TEXT NOT NULL DEFAULT '{}',       -- structured JSON behind the markdown
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Builder security: targets the boss has explicitly authorized for authorized-lab practice.
-- Nothing is a valid target unless it's here and active; this is how "only my own / practice targets" is
-- enforced in code, not by trusting the model.
CREATE TABLE IF NOT EXISTS lab_authorizations (
    id            INTEGER PRIMARY KEY,
    target        TEXT NOT NULL,                  -- a host, VM, CTF name or local app the boss confirmed is his/practice
    kind          TEXT NOT NULL DEFAULT 'lab',    -- ctf | vm | local-app | file | lab
    note          TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    authorized_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    revoked_at    TEXT
);

-- Builder security: an append-only audit log of every gated decision (allowed, refused, approved, denied)
-- and every lab authorization change, so the boss can always see exactly what Builder did and why.
CREATE TABLE IF NOT EXISTS safety_log (
    id          INTEGER PRIMARY KEY,
    action      TEXT NOT NULL,                    -- destructive | network | install | important_files | offense | target_check | lab_mode
    decision    TEXT NOT NULL,                    -- allowed | refused | approved | denied | enabled | disabled
    detail      TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# How sure Scholar is allowed to sound. Assigned by code from the evidence, not by the model.
STATUSES = ("verified", "reported", "assumption", "disputed", "refuted")
# Security-review severity (defensive audits). Assigned by code from the model's labelled output, repaired to "med".
SEVERITIES = ("high", "med", "low")
# A severity maps to a confidence number so the hub's finding display still means something.
SEVERITY_CONFIDENCE = {"high": 0.9, "med": 0.6, "low": 0.3}
LESSON_KINDS = ("preference", "correction", "strategy", "source_note")


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _fts_query(text: str) -> str:
    """Turn free text into a forgiving FTS5 query (any word, prefix match)."""
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2]
    return " OR ".join(f'"{w}"*' for w in words)


def _finding(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["source_ids"] = json.loads(d["source_ids"] or "[]")
    d["checks"] = json.loads(d.get("checks") or "[]")
    return d


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            # Readers (the dashboard) and a writer (background research) can work at the same time.
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Small, additive schema upgrades for databases created before a column existed."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(findings)")}
        with self.conn:
            if "severity" not in have:
                self.conn.execute("ALTER TABLE findings ADD COLUMN severity TEXT")
            if "fix" not in have:
                self.conn.execute("ALTER TABLE findings ADD COLUMN fix TEXT")

    def close(self) -> None:
        self.conn.close()

    def _insert(self, sql: str, params: tuple) -> int:
        with self.conn:
            return self.conn.execute(sql, params).lastrowid

    def _search(self, table: str, query: str, limit: int, extra_sql: str = "",
                extra: list | None = None) -> list[dict]:
        q, extra = _fts_query(query), extra or []
        if q:
            sql = f"""SELECT t.* FROM {table}_fts f JOIN {table} t ON t.id = f.rowid
                      WHERE {table}_fts MATCH ? {extra_sql} ORDER BY bm25({table}_fts) LIMIT ?"""
            return _rows(self.conn.execute(sql, [q, *extra, limit]))
        sql = f"SELECT t.* FROM {table} t WHERE 1=1 {extra_sql} ORDER BY t.id DESC LIMIT ?"
        return _rows(self.conn.execute(sql, [*extra, limit]))

    # ------------------------------------------------------------------ memory
    def remember(self, content: str, topic: str = "general", kind: str = "note") -> int:
        return self._insert(
            "INSERT INTO memories(content, topic, kind) VALUES (?, ?, ?)", (content, topic, kind)
        )

    def forget(self, memory_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

    def recall(self, query: str = "", limit: int = 8) -> list[dict]:
        return self._search("memories", query, limit)

    # ----------------------------------------------------------------- sources
    def add_source(self, url: str, title: str = "", domain: str = "", kind: str = "web",
                   tier: int = 3, text: str = "", published: str = "") -> int:
        """Insert or refresh a source; returns its id (stable per URL)."""
        with self.conn:
            self.conn.execute(
                """INSERT INTO sources(url, title, domain, kind, tier, text, published)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     title = COALESCE(NULLIF(excluded.title, ''), title),
                     text = CASE WHEN length(excluded.text) > length(COALESCE(text, ''))
                                 THEN excluded.text ELSE text END,
                     published = COALESCE(NULLIF(excluded.published, ''), published),
                     fetched_at = datetime('now','localtime')""",
                (url, title, domain, kind, tier, text, published),
            )
        return self.conn.execute("SELECT id FROM sources WHERE url = ?", (url,)).fetchone()[0]

    def get_source(self, source_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return dict(row) if row else None

    def sources(self, query: str = "", limit: int = 20) -> list[dict]:
        return self._search("sources", query, limit)

    # ---------------------------------------------------------------- findings
    def add_finding(self, claim: str, status: str = "assumption", confidence: float = 0.5,
                    topic: str = "general", source_ids: list[int] | None = None,
                    notes: str = "", investigation_id: int | None = None,
                    severity: str | None = None, fix: str | None = None) -> int:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        fid = self._insert(
            """INSERT INTO findings(claim, status, confidence, topic, source_ids, notes, investigation_id,
                                    severity, fix)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (claim, status, _clamp(confidence), topic, json.dumps(sorted(set(source_ids or []))), notes,
             investigation_id, severity, fix),
        )
        self._history(fid, status, _clamp(confidence), "first recorded")
        return fid

    def add_review_finding(self, issue: str, severity: str = "med", fix: str = "",
                           topic: str = "security", source_ids: list[int] | None = None) -> int:
        """Record a defensive security-review finding (issue + severity + recommended fix).

        Stored in the same ``findings`` table as research findings, but with ``severity``/``fix`` set and a
        neutral ``status`` of 'reported'. ``confidence`` reflects the severity so the hub's display is meaningful.
        """
        severity = severity if severity in SEVERITIES else "med"
        return self.add_finding(issue, status="reported", confidence=SEVERITY_CONFIDENCE[severity],
                                topic=topic, source_ids=source_ids, notes="security review",
                                severity=severity, fix=fix)

    def update_finding(self, finding_id: int, status: str | None = None,
                       confidence: float | None = None, add_source_ids: list[int] | None = None,
                       reason: str = "") -> dict | None:
        """Revise a conclusion when new evidence appears; the old state goes to history."""
        f = self.get_finding(finding_id)
        if f is None:
            return None
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        new_status = status or f["status"]
        new_conf = f["confidence"] if confidence is None else _clamp(confidence)
        ids = sorted(set(f["source_ids"]) | set(add_source_ids or []))
        with self.conn:
            self.conn.execute(
                """UPDATE findings SET status = ?, confidence = ?, source_ids = ?,
                     notes = TRIM(notes || ' ' || ?), updated_at = datetime('now','localtime')
                   WHERE id = ?""",
                (new_status, new_conf, json.dumps(ids), reason, finding_id),
            )
        self._history(finding_id, new_status, new_conf, reason)
        return self.get_finding(finding_id)

    def _history(self, finding_id: int, status: str, confidence: float, reason: str) -> None:
        self._insert(
            "INSERT INTO finding_history(finding_id, status, confidence, reason) VALUES (?, ?, ?, ?)",
            (finding_id, status, confidence, reason),
        )

    def get_finding(self, finding_id: int) -> dict | None:
        return _finding(self.conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone())

    def findings(self, query: str = "", limit: int = 20, status: str | None = None) -> list[dict]:
        extra_sql, extra = ("AND t.status = ?", [status]) if status else ("", [])
        return [_finding(r) for r in self._search("findings", query, limit, extra_sql, extra)]

    def finding_history(self, finding_id: int) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT * FROM finding_history WHERE finding_id = ? ORDER BY id", (finding_id,)
        ))

    # -------------------------------------------------------- security: lab authorizations + audit log
    def authorize_lab_target(self, target: str, kind: str = "lab", note: str = "") -> int:
        """Record the boss's authorization of a practice target. Returns its id."""
        return self._insert(
            "INSERT INTO lab_authorizations(target, kind, note) VALUES (?, ?, ?)",
            (target.strip(), kind, note),
        )

    def is_lab_target_authorized(self, target: str) -> bool:
        if not target or not str(target).strip():
            return False
        row = self.conn.execute(
            "SELECT 1 FROM lab_authorizations WHERE active = 1 AND target = ? LIMIT 1",
            (str(target).strip(),)).fetchone()
        return row is not None

    def lab_targets(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM lab_authorizations"
        if active_only:
            sql += " WHERE active = 1"
        return _rows(self.conn.execute(sql + " ORDER BY id DESC"))

    def revoke_lab_targets(self, target: str | None = None) -> int:
        """Deactivate one target (or all when target is None). Returns how many were revoked."""
        with self.conn:
            if target is None:
                cur = self.conn.execute(
                    "UPDATE lab_authorizations SET active = 0, revoked_at = datetime('now','localtime') WHERE active = 1")
            else:
                cur = self.conn.execute(
                    "UPDATE lab_authorizations SET active = 0, revoked_at = datetime('now','localtime') "
                    "WHERE active = 1 AND target = ?", (str(target).strip(),))
            return cur.rowcount

    def log_safety(self, action: str, decision: str, detail: str = "", target: str = "", reason: str = "") -> int:
        return self._insert(
            "INSERT INTO safety_log(action, decision, detail, target, reason) VALUES (?, ?, ?, ?, ?)",
            (action, decision, detail, target, reason),
        )

    def safety_events(self, limit: int = 50) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM safety_log ORDER BY id DESC LIMIT ?", (limit,)))

    # ----------------------------------------------------------------- reports
    def add_report(self, question: str, body: str, mode: str = "research", data: dict | None = None) -> int:
        return self._insert(
            "INSERT INTO reports(question, mode, body, data) VALUES (?, ?, ?, ?)",
            (question, mode, body, json.dumps(data or {}, default=str)),
        )

    def get_report(self, report_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        return d

    def reports(self, limit: int = 20) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT id, question, mode, created_at FROM reports ORDER BY id DESC LIMIT ?", (limit,)
        ))

    def set_checks(self, finding_id: int, checks: list[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE findings SET checks = ? WHERE id = ?", (json.dumps(checks), finding_id))

    def add_evidence(self, finding_id: int, source_id: int, quote: str = "", quote_ok: bool = False,
                     stance: str = "supports", note: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO evidence(finding_id, source_id, stance, quote, quote_ok, note)
                   VALUES (?, ?, ?, ?, ?, ?)""", (finding_id, source_id, stance, quote, int(quote_ok), note))

    def evidence(self, finding_id: int) -> list[dict]:
        return _rows(self.conn.execute(
            """SELECT e.*, s.title, s.url, s.tier, s.domain, s.published FROM evidence e
               LEFT JOIN sources s ON s.id = e.source_id WHERE e.finding_id = ? ORDER BY e.stance, e.id""",
            (finding_id,)))

    # ------------------------------------------------------------------- graph
    def entity(self, name: str, type: str = "thing", description: str = "") -> int:
        name = " ".join(name.split())[:200]
        with self.conn:
            self.conn.execute(
                """INSERT INTO entities(name, type, description) VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     type = CASE WHEN type = 'thing' THEN excluded.type ELSE type END,
                     description = CASE WHEN description = '' THEN excluded.description ELSE description END""",
                (name, type or "thing", description))
        return self.conn.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()[0]

    def get_entity(self, ident: int | str) -> dict | None:
        col = "id" if isinstance(ident, int) or str(ident).isdigit() else "name"
        row = self.conn.execute(f"SELECT * FROM entities WHERE {col} = ?", (ident,)).fetchone()
        return dict(row) if row else None

    def mention(self, entity_id: int, source_id: int, investigation_id: int | None = None) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO mentions VALUES (?, ?, ?)",
                              (entity_id, source_id, investigation_id))

    def relate(self, subject: str, predicate: str, obj: str, source_id: int | None = None, date: str = "",
               investigation_id: int | None = None, subject_type: str = "thing", object_type: str = "thing") -> int:
        s, o = self.entity(subject, subject_type), self.entity(obj, object_type)
        pred = re.sub(r"[^a-z0-9]+", "_", predicate.lower()).strip("_")[:60] or "related_to"
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO relations(subject_id, predicate, object_id, source_id, date, investigation_id)
                   VALUES (?, ?, ?, ?, ?, ?)""", (s, pred, o, source_id, date, investigation_id))
        for e in (s, o):
            if source_id:
                self.mention(e, source_id, investigation_id)
        return self.conn.execute(
            "SELECT id FROM relations WHERE subject_id=? AND predicate=? AND object_id=? AND source_id IS ?",
            (s, pred, o, source_id)).fetchone()[0]

    def neighbors(self, ident: int | str) -> dict:
        e = self.get_entity(ident)
        if not e:
            return {}
        rel = _rows(self.conn.execute(
            """SELECT r.id, a.name AS subject, r.predicate, b.name AS object, r.source_id, r.date
               FROM relations r JOIN entities a ON a.id = r.subject_id JOIN entities b ON b.id = r.object_id
               WHERE r.subject_id = ? OR r.object_id = ? ORDER BY r.id""", (e["id"], e["id"])))
        sources = [r[0] for r in self.conn.execute(
            "SELECT source_id FROM mentions WHERE entity_id = ?", (e["id"],))]
        return {**e, "relations": rel, "source_ids": sources}

    def entities(self, query: str = "", limit: int = 50, investigation_id: int | None = None,
                 unexplored: bool = False) -> list[dict]:
        sql = """SELECT e.*, COUNT(DISTINCT m.source_id) AS sources FROM entities e
                 LEFT JOIN mentions m ON m.entity_id = e.id WHERE 1=1"""
        params: list[Any] = []
        if query:
            sql += " AND (e.name LIKE ? OR e.description LIKE ?)"
            params += [f"%{query}%", f"%{query}%"]
        if investigation_id is not None:
            sql += " AND m.investigation_id = ?"
            params.append(investigation_id)
        if unexplored:
            sql += " AND e.explored = 0"
        sql += " GROUP BY e.id ORDER BY sources DESC, e.id LIMIT ?"
        return _rows(self.conn.execute(sql, [*params, limit]))

    def mark_explored(self, entity_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE entities SET explored = 1 WHERE id = ?", (entity_id,))

    def relations(self, investigation_id: int | None = None, limit: int = 200) -> list[dict]:
        where, params = ("WHERE r.investigation_id = ?", [investigation_id]) if investigation_id else ("", [])
        return _rows(self.conn.execute(
            f"""SELECT r.id, a.name AS subject, a.type AS subject_type, r.predicate, b.name AS object,
                       b.type AS object_type, r.source_id, r.date FROM relations r
                JOIN entities a ON a.id = r.subject_id JOIN entities b ON b.id = r.object_id
                {where} ORDER BY r.id LIMIT ?""", [*params, limit]))

    # ---------------------------------------------------- investigations
    def start_investigation(self, question: str, mode: str = "research", depth: str = "standard") -> int:
        return self._insert("INSERT INTO investigations(question, mode, depth) VALUES (?, ?, ?)",
                            (question, mode, depth))

    def finish_investigation(self, inv_id: int, status: str, summary: str = "", report_id: int | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE investigations SET status = ?, summary = ?, report_id = ?,
                     finished_at = datetime('now','localtime') WHERE id = ?""", (status, summary, report_id, inv_id))

    def investigations(self, limit: int = 20) -> list[dict]:
        return _rows(self.conn.execute(
            """SELECT i.*, (SELECT COUNT(*) FROM questions q WHERE q.investigation_id = i.id) AS questions,
                      (SELECT COUNT(*) FROM questions q WHERE q.investigation_id = i.id AND q.status = 'open') AS open
               FROM investigations i ORDER BY i.id DESC LIMIT ?""", (limit,)))

    def get_investigation(self, inv_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM investigations WHERE id = ?", (inv_id,)).fetchone()
        return dict(row) if row else None

    def add_question(self, inv_id: int, text: str, parent_id: int | None = None, dimension: str = "",
                     level: int = 1, origin: str = "plan") -> int | None:
        """Add a question unless an equivalent one is already in this investigation."""
        key = _norm_q(text)
        for (existing,) in self.conn.execute("SELECT text FROM questions WHERE investigation_id = ?", (inv_id,)):
            if _norm_q(existing) == key:
                return None
        return self._insert(
            "INSERT INTO questions(investigation_id, parent_id, text, dimension, level, origin) VALUES (?,?,?,?,?,?)",
            (inv_id, parent_id, text.strip(), dimension, level, origin))

    def answer_question(self, qid: int, status: str, answer: str = "") -> None:
        with self.conn:
            self.conn.execute("UPDATE questions SET status = ?, answer = ? WHERE id = ?", (status, answer, qid))

    def questions(self, inv_id: int | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
        sql, params = "SELECT * FROM questions WHERE 1=1", []
        if inv_id is not None:
            sql += " AND investigation_id = ?"
            params.append(inv_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        return _rows(self.conn.execute(sql + " ORDER BY id LIMIT ?", [*params, limit]))

    def log_query(self, text: str, backend: str, hits: int, inv_id: int | None = None,
                  question_id: int | None = None) -> int:
        return self._insert("INSERT INTO queries(investigation_id, question_id, text, backend, hits) VALUES (?,?,?,?,?)",
                            (inv_id, question_id, text, backend, hits))

    def credit_query(self, query_id: int, facts: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE queries SET facts = facts + ? WHERE id = ?", (facts, query_id))

    def useful_queries(self, limit: int = 50) -> list[dict]:
        return _rows(self.conn.execute(
            "SELECT * FROM queries WHERE facts > 0 ORDER BY facts DESC, id DESC LIMIT ?", (limit,)))

    # -------------------------------------------------------- tables, terms
    def add_table(self, source_id: int, idx: int, caption: str, n_rows: int, n_cols: int, csv_path: str,
                  preview: str) -> int:
        with self.conn:
            self.conn.execute(
                """INSERT INTO doc_tables(source_id, idx, caption, n_rows, n_cols, csv_path, preview)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_id, idx) DO UPDATE SET
                   caption=excluded.caption, csv_path=excluded.csv_path, preview=excluded.preview""",
                (source_id, idx, caption, n_rows, n_cols, csv_path, preview))
        return self.conn.execute("SELECT id FROM doc_tables WHERE source_id=? AND idx=?", (source_id, idx)).fetchone()[0]

    def tables(self, source_id: int | None = None, limit: int = 100) -> list[dict]:
        where, params = ("WHERE source_id = ?", [source_id]) if source_id else ("", [])
        return _rows(self.conn.execute(f"SELECT * FROM doc_tables {where} ORDER BY id LIMIT ?", [*params, limit]))

    def get_table(self, table_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM doc_tables WHERE id = ?", (table_id,)).fetchone()
        return dict(row) if row else None

    def define(self, term: str, definition: str, domain: str = "general", source_id: int | None = None) -> int:
        with self.conn:
            self.conn.execute(
                """INSERT INTO glossary(term, definition, domain, source_id) VALUES (?, ?, ?, ?)
                   ON CONFLICT(term) DO NOTHING""", (term.strip()[:120], definition.strip(), domain, source_id))
        return self.conn.execute("SELECT id FROM glossary WHERE term = ?", (term.strip()[:120],)).fetchone()[0]

    def glossary(self, query: str = "", limit: int = 100) -> list[dict]:
        if query:
            return _rows(self.conn.execute(
                "SELECT * FROM glossary WHERE term LIKE ? OR definition LIKE ? ORDER BY term LIMIT ?",
                (f"%{query}%", f"%{query}%", limit)))
        return _rows(self.conn.execute("SELECT * FROM glossary ORDER BY term LIMIT ?", (limit,)))

    # ------------------------------------------------------------- lessons
    def add_lesson(self, text: str, kind: str = "preference", topic: str = "general", origin: str = "user",
                   ref: str = "") -> int:
        if kind not in LESSON_KINDS:
            raise ValueError(f"kind must be one of {', '.join(LESSON_KINDS)}")
        if origin not in ("user", "verified"):
            raise ValueError("lessons come only from the user or from verified outcomes")
        return self._insert("INSERT INTO lessons(kind, text, topic, origin, ref) VALUES (?, ?, ?, ?, ?)",
                            (kind, text, topic, origin, ref))

    def lessons(self, kind: str | None = None, active_only: bool = True, limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM lessons WHERE 1=1", []
        if active_only:
            sql += " AND active = 1"
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return _rows(self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*params, limit]))

    def retire_lesson(self, lesson_id: int) -> bool:
        with self.conn:
            return self.conn.execute("UPDATE lessons SET active = 0 WHERE id = ?", (lesson_id,)).rowcount > 0

    # ------------------------------------------------- pending work, inbox
    def add_pending(self, kind: str, text: str, depth: str = "quick", origin: str = "", ref: str = "",
                    asked_at: str | None = None) -> int:
        return self._insert(
            """INSERT INTO pending(kind, text, depth, origin, ref, asked_at)
               VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now','localtime')))""",
            (kind, text, depth, origin, ref, asked_at))

    def pending(self, include_done: bool = False, limit: int = 100) -> list[dict]:
        where = "" if include_done else "WHERE done_at IS NULL"
        return _rows(self.conn.execute(f"SELECT * FROM pending {where} ORDER BY id LIMIT ?", (limit,)))

    def finish_pending(self, pid: int, result: str = "") -> None:
        with self.conn:
            self.conn.execute("UPDATE pending SET done_at = datetime('now','localtime'), result = ? WHERE id = ?",
                              (result, pid))

    def notify(self, text: str, ref: str = "") -> int:
        return self._insert("INSERT INTO inbox(text, ref) VALUES (?, ?)", (text, ref))

    def inbox(self, unread_only: bool = False, limit: int = 30) -> list[dict]:
        where = "WHERE read = 0" if unread_only else ""
        return _rows(self.conn.execute(f"SELECT * FROM inbox {where} ORDER BY id DESC LIMIT ?", (limit,)))

    def mark_read(self, msg_id: int | None = None) -> int:
        with self.conn:
            if msg_id is None:
                return self.conn.execute("UPDATE inbox SET read = 1 WHERE read = 0").rowcount
            return self.conn.execute("UPDATE inbox SET read = 1 WHERE id = ?", (msg_id,)).rowcount

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        by_status = dict(self.conn.execute(
            "SELECT status, COUNT(*) FROM findings GROUP BY status").fetchall())
        return {
            "memories": one("SELECT COUNT(*) FROM memories"),
            "sources": one("SELECT COUNT(*) FROM sources"),
            "findings": one("SELECT COUNT(*) FROM findings"),
            "findings_by_status": {s: by_status.get(s, 0) for s in STATUSES},
            "reports": one("SELECT COUNT(*) FROM reports"),
            "investigations": one("SELECT COUNT(*) FROM investigations"),
            "open_questions": one("SELECT COUNT(*) FROM questions WHERE status = 'open'"),
            "entities": one("SELECT COUNT(*) FROM entities"),
            "relations": one("SELECT COUNT(*) FROM relations"),
            "tables": one("SELECT COUNT(*) FROM doc_tables"),
            "glossary": one("SELECT COUNT(*) FROM glossary"),
            "lessons": one("SELECT COUNT(*) FROM lessons WHERE active = 1"),
            "useful_queries": one("SELECT COUNT(*) FROM queries WHERE facts > 0"),
            "pending": one("SELECT COUNT(*) FROM pending WHERE done_at IS NULL"),
            "unread": one("SELECT COUNT(*) FROM inbox WHERE read = 0"),
        }


def _norm_q(text: str) -> str:
    return " ".join(w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP)


_STOP = {"the", "a", "an", "of", "for", "to", "in", "is", "are", "what", "how", "and", "or", "does", "do"}


def _clamp(x: float) -> float:
    return round(min(1.0, max(0.0, float(x))), 2)
