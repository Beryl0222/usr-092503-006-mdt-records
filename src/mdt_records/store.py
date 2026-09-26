"""SQLite 持久化层。

只负责 schema、连接管理与事务原语；业务规则位于 ``workflow`` 模块。
单连接配合 ``BEGIN IMMEDIATE`` 串行化写入，条件更新（CAS）由业务层
在事务内完成，从而保证并发下“只成功一次”。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id           TEXT PRIMARY KEY,
    display_name      TEXT NOT NULL,
    role              TEXT NOT NULL,            -- coordinator | specialist | patient | auditor
    authorized_signer INTEGER NOT NULL DEFAULT 0,
    active            INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS user_disciplines (
    user_id    TEXT NOT NULL,
    discipline TEXT NOT NULL,
    PRIMARY KEY (user_id, discipline)
);
CREATE TABLE IF NOT EXISTS tokens (
    token   TEXT PRIMARY KEY,
    user_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegations (
    delegation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    granter_id    TEXT NOT NULL,
    grantee_id    TEXT NOT NULL,
    scope         TEXT NOT NULL,                -- opinion | sign
    discipline    TEXT,
    case_key      TEXT,
    granted_at    REAL NOT NULL,
    expires_at    REAL,
    revoked_at    REAL
);
CREATE TABLE IF NOT EXISTS cases (
    case_key   TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS case_assignments (
    case_key   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    discipline TEXT NOT NULL,
    PRIMARY KEY (case_key, user_id)
);
CREATE TABLE IF NOT EXISTS patient_links (
    case_key    TEXT NOT NULL,
    patient_ref TEXT NOT NULL,
    linked_at   REAL NOT NULL,
    PRIMARY KEY (case_key, patient_ref)
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    content     TEXT,
    sensitive   INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    uploaded_at REAL NOT NULL,
    uploaded_by TEXT NOT NULL,
    UNIQUE (case_key, kind, version)
);
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key     TEXT NOT NULL,
    emergency    INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | in_session | closed
    scheduled_at REAL NOT NULL,
    created_by   TEXT NOT NULL,
    closed_at    REAL
);
CREATE TABLE IF NOT EXISTS meeting_required (
    meeting_id INTEGER NOT NULL,
    discipline TEXT NOT NULL,
    PRIMARY KEY (meeting_id, discipline)
);
CREATE TABLE IF NOT EXISTS opinions (
    opinion_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id        INTEGER NOT NULL,
    discipline        TEXT NOT NULL,
    author_id         TEXT NOT NULL,
    on_behalf_of      TEXT,
    delegation_id     INTEGER,
    body              TEXT NOT NULL,
    evidence_refs     TEXT NOT NULL,               -- JSON: [{"kind":..,"version":..}]
    coi_disclosed     INTEGER NOT NULL DEFAULT 0,
    coi_detail        TEXT,
    state             TEXT NOT NULL DEFAULT 'draft', -- draft | confirmed | invalidated
    created_at        REAL NOT NULL,
    confirmed_at      REAL,
    invalidated_at    REAL,
    invalidate_reason TEXT,
    new_evidence_id   INTEGER
);
CREATE TABLE IF NOT EXISTS absence_exceptions (
    exception_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      INTEGER NOT NULL,
    discipline      TEXT NOT NULL,
    reason          TEXT NOT NULL,
    granted_by      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    review_deadline REAL NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected | overdue
    reviewed_at     REAL,
    reviewer_id     TEXT,
    late            INTEGER NOT NULL DEFAULT 0,
    review_note     TEXT
);
CREATE TABLE IF NOT EXISTS candidate_plans (
    candidate_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id     INTEGER NOT NULL,
    case_key       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'candidate', -- candidate | invalidated | issued
    snapshot       TEXT NOT NULL,                     -- JSON，见 workflow._build_snapshot
    formed_at      REAL NOT NULL,
    formed_by      TEXT NOT NULL,
    invalidated_at REAL,
    invalidate_reason TEXT,
    new_evidence_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_candidate_meeting ON candidate_plans(meeting_id);
-- 同一会议同时只允许一版“候选中”的方案；改版作废旧版后才能形成新版。
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_one_active
    ON candidate_plans(meeting_id) WHERE status='candidate';
CREATE TABLE IF NOT EXISTS issued_plans (
    plan_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id  INTEGER NOT NULL UNIQUE,
    meeting_id    INTEGER NOT NULL,
    case_key      TEXT NOT NULL,
    signer_id     TEXT NOT NULL,
    on_behalf_of  TEXT,
    delegation_id INTEGER,
    signed_at     REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'      -- active | consent_withdrawn | superseded
);
CREATE TABLE IF NOT EXISTS consents (
    consent_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key            TEXT NOT NULL,
    plan_id             INTEGER NOT NULL UNIQUE,
    preference_version  INTEGER NOT NULL,
    patient_ref         TEXT NOT NULL,
    status              TEXT NOT NULL,                -- granted | withdrawn | superseded
    granted_at          REAL NOT NULL,
    withdrawn_at        REAL,
    superseded_at       REAL
);
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    resource    TEXT,
    case_key    TEXT,
    result      TEXT NOT NULL,                        -- allowed | denied
    sensitive   INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    body_json   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (idem_key, user_id)
);
"""


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class Store:
    """封装 SQLite 连接；通过锁与立即事务提供可串行化的写入。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = _dict_factory
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.init_schema()

    def init_schema(self) -> None:
        # executescript 会隐式提交，不能包在显式事务里；CREATE TABLE IF
        # NOT EXISTS 本身可安全重复执行。
        with self._lock:
            self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """开启立即写事务；持锁期间完成读-判定-写，杜绝交错提交。"""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def one(self, sql: str, params: dict | tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: dict | tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
