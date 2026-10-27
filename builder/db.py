"""Builder's local store (SQLite). Everything lives in one file (``~/.builder/builder.db``):

- **memories** — short notes about the boss (his stack, preferences) used to steer replies;
- **reviews** — defensive security-review findings (issue + severity + fix), Builder's own security store;
- **lessons** — what he's learned from the boss's feedback (preferences, corrections, strategies);
- **lab_authorizations** + **safety_log** — the authorized-lab targets and the audit trail of every gated
  decision (see safety.py);
- **pending** — work parked while offline, to resume when the internet is back;
- **inbox** — messages for the boss, shown on the dashboard.

It's plain standard-library sqlite3 with simple LIKE search — no extra software, works offline.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY,
    content     TEXT NOT NULL,
    topic       TEXT NOT NULL DEFAULT 'general',
    kind        TEXT NOT NULL DEFAULT 'note',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Builder's own defensive security-review findings (kept separate from anything else).
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY,
    issue       TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'med',    -- high | med | low
    fix         TEXT NOT NULL DEFAULT '',
    topic       TEXT NOT NULL DEFAULT 'security',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Build memory: what Builder has built, whether it passed, and the problem he had to solve — so he gets
-- better at building and at security over time by remembering past projects and fixes.
CREATE TABLE IF NOT EXISTS builds (
    id          INTEGER PRIMARY KEY,
    task        TEXT NOT NULL,
    dir         TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 0,     -- 1 = tests passed
    attempts    INTEGER NOT NULL DEFAULT 1,
    problem     TEXT NOT NULL DEFAULT '',       -- the error he hit (and solved, if ok) — a short signature
    files       TEXT NOT NULL DEFAULT '',       -- JSON of the working files when it passed, to reuse/adapt next time
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Learning from experience: the boss's feedback and preferences.
CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                  -- preference | correction | strategy | source_note
    text        TEXT NOT NULL,
    topic       TEXT NOT NULL DEFAULT 'general',
    origin      TEXT NOT NULL DEFAULT 'user',   -- user | verified
    ref         TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Security: targets the boss has authorized for authorized-lab practice (see safety.py).
CREATE TABLE IF NOT EXISTS lab_authorizations (
    id            INTEGER PRIMARY KEY,
    target        TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'lab',    -- ctf | vm | local-app | file | lab
    note          TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    authorized_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    revoked_at    TEXT
);

-- Security: an append-only audit log of every gated decision and authorization change.
CREATE TABLE IF NOT EXISTS safety_log (
    id          INTEGER PRIMARY KEY,
    action      TEXT NOT NULL,                    -- destructive | network | install | important_files | offense | target_check | lab_mode
    decision    TEXT NOT NULL,                    -- allowed | refused | approved | denied | enabled | disabled
    detail      TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Work parked while offline, picked up when the internet returns.
CREATE TABLE IF NOT EXISTS pending (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    depth       TEXT NOT NULL DEFAULT 'quick',
    origin      TEXT NOT NULL DEFAULT '',
    ref         TEXT NOT NULL DEFAULT '',
    asked_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    done_at     TEXT,
    result      TEXT NOT NULL DEFAULT ''
);

-- Messages for the boss (shown on the dashboard).
CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,
    ref         TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    read        INTEGER NOT NULL DEFAULT 0
);
"""

# Security-review severity. Assigned by code from the model's labelled output, repaired to "med".
SEVERITIES = ("high", "med", "low")
# A severity maps to a rough confidence number for display.
SEVERITY_CONFIDENCE = {"high": 0.9, "med": 0.6, "low": 0.3}
LESSON_KINDS = ("preference", "correction", "strategy", "source_note")


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _like(query: str) -> str:
    return "%" + re.sub(r"\s+", "%", query.strip()) + "%"


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Small, additive upgrades for databases created before a column existed."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(builds)")}
        if "files" not in have:
            with self.conn:
                self.conn.execute("ALTER TABLE builds ADD COLUMN files TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        self.conn.close()

    def _insert(self, sql: str, params: tuple) -> int:
        with self.conn:
            return self.conn.execute(sql, params).lastrowid

    # ------------------------------------------------------------------ memories (notes about the boss)
    def remember(self, content: str, topic: str = "general", kind: str = "note") -> int:
        return self._insert("INSERT INTO memories(content, topic, kind) VALUES (?, ?, ?)",
                            (content, topic, kind))

    def forget(self, memory_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

    def recall(self, query: str = "", limit: int = 8) -> list[dict]:
        if query.strip():
            return _rows(self.conn.execute(
                "SELECT * FROM memories WHERE content LIKE ? ORDER BY id DESC LIMIT ?", (_like(query), limit)))
        return _rows(self.conn.execute("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)))

    # ------------------------------------------------------------------ security reviews
    def add_review(self, issue: str, severity: str = "med", fix: str = "", topic: str = "security") -> int:
        severity = severity if severity in SEVERITIES else "med"
        return self._insert("INSERT INTO reviews(issue, severity, fix, topic) VALUES (?, ?, ?, ?)",
                            (issue, severity, fix, topic))

    # Back-compat name used by the connector/CLI; same thing.
    def add_review_finding(self, issue: str, severity: str = "med", fix: str = "",
                           topic: str = "security", source_ids=None) -> int:
        return self.add_review(issue, severity, fix, topic)

    def review_exists(self, issue: str) -> bool:
        return self.conn.execute("SELECT 1 FROM reviews WHERE issue = ? LIMIT 1", (issue,)).fetchone() is not None

    def reviews(self, query: str = "", severity: str | None = None, limit: int = 50) -> list[dict]:
        sql, params = "SELECT * FROM reviews WHERE 1=1", []
        if query.strip():
            sql += " AND issue LIKE ?"; params.append(_like(query))
        if severity:
            sql += " AND severity = ?"; params.append(severity)
        sql += " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END, id DESC LIMIT ?"
        params.append(limit)
        return _rows(self.conn.execute(sql, params))

    # ------------------------------------------------------------------ build memory
    def add_build(self, task: str, dir: str = "", ok: bool = False, attempts: int = 1, problem: str = "",
                  files: str = "") -> int:
        return self._insert("INSERT INTO builds(task, dir, ok, attempts, problem, files) VALUES (?, ?, ?, ?, ?, ?)",
                            (task, dir, 1 if ok else 0, attempts, problem[:500], files))

    def passed_builds_with_files(self, limit: int = 40) -> list[dict]:
        """Recent builds that PASSED and saved their working files — the pool to reuse/adapt from."""
        return _rows(self.conn.execute(
            "SELECT * FROM builds WHERE ok = 1 AND files != '' ORDER BY id DESC LIMIT ?", (limit,)))

    def builds(self, query: str = "", limit: int = 20) -> list[dict]:
        if query.strip():
            return _rows(self.conn.execute(
                "SELECT * FROM builds WHERE task LIKE ? OR problem LIKE ? ORDER BY id DESC LIMIT ?",
                (_like(query), _like(query), limit)))
        return _rows(self.conn.execute("SELECT * FROM builds ORDER BY id DESC LIMIT ?", (limit,)))

    # ------------------------------------------------------------------ lessons
    def add_lesson(self, text: str, kind: str = "preference", topic: str = "general",
                   origin: str = "user", ref: str = "") -> int:
        if kind not in LESSON_KINDS:
            raise ValueError(f"kind must be one of {', '.join(LESSON_KINDS)}")
        return self._insert("INSERT INTO lessons(kind, text, topic, origin, ref) VALUES (?, ?, ?, ?, ?)",
                            (kind, text, topic, origin, ref))

    def lessons(self, kind: str | None = None, active_only: bool = True, limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM lessons WHERE 1=1", []
        if active_only:
            sql += " AND active = 1"
        if kind:
            sql += " AND kind = ?"; params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
        return _rows(self.conn.execute(sql, params))

    def retire_lesson(self, lesson_id: int) -> bool:
        with self.conn:
            return self.conn.execute("UPDATE lessons SET active = 0 WHERE id = ?", (lesson_id,)).rowcount > 0

    # ------------------------------------------------------- security: lab authorizations + audit log
    def authorize_lab_target(self, target: str, kind: str = "lab", note: str = "") -> int:
        return self._insert("INSERT INTO lab_authorizations(target, kind, note) VALUES (?, ?, ?)",
                            (target.strip(), kind, note))

    def is_lab_target_authorized(self, target: str) -> bool:
        if not target or not str(target).strip():
            return False
        return self.conn.execute(
            "SELECT 1 FROM lab_authorizations WHERE active = 1 AND target = ? LIMIT 1",
            (str(target).strip(),)).fetchone() is not None

    def lab_targets(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM lab_authorizations" + (" WHERE active = 1" if active_only else "")
        return _rows(self.conn.execute(sql + " ORDER BY id DESC"))

    def revoke_lab_targets(self, target: str | None = None) -> int:
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
        return self._insert("INSERT INTO safety_log(action, decision, detail, target, reason) VALUES (?, ?, ?, ?, ?)",
                            (action, decision, detail, target, reason))

    def safety_events(self, limit: int = 50) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM safety_log ORDER BY id DESC LIMIT ?", (limit,)))

    # ------------------------------------------------------------------ offline pending
    def add_pending(self, kind: str, text: str, depth: str = "quick", origin: str = "", ref: str = "",
                    asked_at: str | None = None) -> int:
        if asked_at:
            return self._insert("INSERT INTO pending(kind, text, depth, origin, ref, asked_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (kind, text, depth, origin, ref, asked_at))
        return self._insert("INSERT INTO pending(kind, text, depth, origin, ref) VALUES (?, ?, ?, ?, ?)",
                            (kind, text, depth, origin, ref))

    def pending(self, include_done: bool = False, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM pending" + ("" if include_done else " WHERE done_at IS NULL")
        return _rows(self.conn.execute(sql + " ORDER BY id LIMIT ?", (limit,)))

    def finish_pending(self, pid: int, result: str = "") -> None:
        with self.conn:
            self.conn.execute("UPDATE pending SET done_at = datetime('now','localtime'), result = ? WHERE id = ?",
                             (result, pid))

    # ------------------------------------------------------------------ inbox (messages for the boss)
    def notify(self, text: str, ref: str = "") -> int:
        return self._insert("INSERT INTO inbox(text, ref) VALUES (?, ?)", (text, ref))

    def inbox(self, unread_only: bool = False, limit: int = 30) -> list[dict]:
        sql = "SELECT * FROM inbox" + (" WHERE read = 0" if unread_only else "")
        return _rows(self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (limit,)))

    def mark_read(self, msg_id: int | None = None) -> int:
        with self.conn:
            if msg_id is None:
                return self.conn.execute("UPDATE inbox SET read = 1 WHERE read = 0").rowcount
            return self.conn.execute("UPDATE inbox SET read = 1 WHERE id = ?", (msg_id,)).rowcount

    # ------------------------------------------------------------------ dashboard stats
    def stats(self) -> dict:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        by_sev = dict(self.conn.execute("SELECT severity, COUNT(*) FROM reviews GROUP BY severity").fetchall())
        return {
            "memories": one("SELECT COUNT(*) FROM memories"),
            "reviews": one("SELECT COUNT(*) FROM reviews"),
            "reviews_by_severity": {s: by_sev.get(s, 0) for s in SEVERITIES},
            "builds": one("SELECT COUNT(*) FROM builds"),
            "builds_passed": one("SELECT COUNT(*) FROM builds WHERE ok = 1"),
            "lessons": one("SELECT COUNT(*) FROM lessons WHERE active = 1"),
            "lab_targets": one("SELECT COUNT(*) FROM lab_authorizations WHERE active = 1"),
            "pending": one("SELECT COUNT(*) FROM pending WHERE done_at IS NULL"),
            "unread": one("SELECT COUNT(*) FROM inbox WHERE read = 0"),
        }
