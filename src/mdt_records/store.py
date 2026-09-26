"""SQLite 持久化层：单一连接 + 可重入锁，写操作显式事务。

不变量由数据库唯一索引兜底：
- 同一病例同一专业至多一条 confirmed 意见；
- 同一病例同一作者至多一条 draft/confirmed 意见；
- 同一病例至多一个 active 候选方案 / active 签发方案；
- 同一签发方案至多一条 active 知情同意。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    disciplines TEXT NOT NULL DEFAULT '[]',
    secret      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_key             TEXT PRIMARY KEY,
    fertility_preference TEXT NOT NULL,
    created_by           TEXT NOT NULL,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key   TEXT NOT NULL REFERENCES cases(case_key),
    kind       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    content    TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (case_key, kind, version)
);

CREATE TABLE IF NOT EXISTS opinions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key           TEXT NOT NULL REFERENCES cases(case_key),
    discipline         TEXT NOT NULL,
    author             TEXT NOT NULL,
    content            TEXT NOT NULL,
    coi_disclosed      INTEGER NOT NULL,
    coi_detail         TEXT NOT NULL DEFAULT '',
    evidence_snapshot  TEXT NOT NULL,
    state              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    confirmed_at       TEXT,
    invalidated_at     TEXT,
    invalidated_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opinions_confirmed
    ON opinions (case_key, discipline) WHERE state = 'confirmed';
CREATE UNIQUE INDEX IF NOT EXISTS uq_opinions_author_active
    ON opinions (case_key, discipline, author) WHERE state IN ('draft', 'confirmed');

CREATE TABLE IF NOT EXISTS meetings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key   TEXT NOT NULL REFERENCES cases(case_key),
    kind       TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS absence_exceptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   INTEGER NOT NULL REFERENCES meetings(id),
    case_key     TEXT NOT NULL REFERENCES cases(case_key),
    discipline   TEXT NOT NULL,
    deadline     TEXT NOT NULL,
    state        TEXT NOT NULL,
    fulfilled_by TEXT,
    fulfilled_at TEXT,
    late         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS candidate_plans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key          TEXT NOT NULL REFERENCES cases(case_key),
    meeting_id        INTEGER NOT NULL REFERENCES meetings(id),
    evidence_snapshot TEXT NOT NULL,
    confirmations     TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    superseded_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_active
    ON candidate_plans (case_key) WHERE status = 'candidate';

CREATE TABLE IF NOT EXISTS issued_plans (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key           TEXT NOT NULL REFERENCES cases(case_key),
    candidate_id       INTEGER NOT NULL REFERENCES candidate_plans(id),
    evidence_snapshot  TEXT NOT NULL,
    content            TEXT NOT NULL,
    issued_by          TEXT NOT NULL,
    via_delegation_id  INTEGER,
    status             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    invalidated_at     TEXT,
    invalidated_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_issued_active
    ON issued_plans (case_key) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS consents (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id            INTEGER NOT NULL REFERENCES issued_plans(id),
    case_key           TEXT NOT NULL REFERENCES cases(case_key),
    status             TEXT NOT NULL,
    recorded_by        TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    withdrawn_at       TEXT,
    invalidated_at     TEXT,
    invalidated_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_consent_active
    ON consents (plan_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS delegations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    delegator  TEXT NOT NULL,
    delegate   TEXT NOT NULL,
    case_key   TEXT,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key   TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    resource   TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Store:
    """线程安全的 SQLite 访问封装。所有写路径使用 BEGIN IMMEDIATE 串行化。"""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：进入即取写锁，提交或回滚后释放。"""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def read(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def read_one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
