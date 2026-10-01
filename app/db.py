"""SQLite 存储层。

- 单连接 + 进程内可重入锁, 写操作使用 BEGIN IMMEDIATE 串行化;
- meta 表维护资料修订号 (revision), 任何论文/评审人资料变更都会使其 +1;
- published 表保存当前已发布方案、发布序号 (serial, 每次发布/补位发布 +1)
  及其所依据的修订号;
- assignment_decisions 表保存评审人对当前发布版分配关系的确认/回避决定
  (按发布序号隔离);
- submitted_reviews 表保存评审人对"当前发布版中已确认且未回避"槽位的正式评分
  与评语 (按发布序号隔离; 每个有效槽位至多一份, 以收据 receipt 标识;
  补位发布时连续保留的确认槽位连同评语一起复制到新序号);
- reviewer_recusals 表保存评审人声明过的硬回避, 后续普通求解与补位都必须遵守;
- 发布 = 在同一个写事务内 "校验修订号 -> 重算 -> 写入",
  因此与资料修改并发时不会产生基于旧版资料的方案。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.getcwd(), "data", "app.db"))

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id     TEXT PRIMARY KEY,
    manuscript   TEXT NOT NULL,
    topics       TEXT NOT NULL,   -- JSON array
    institutions TEXT NOT NULL    -- JSON array
);
CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id  TEXT PRIMARY KEY,
    credential   TEXT NOT NULL,
    topics       TEXT NOT NULL,   -- JSON array
    institution  TEXT NOT NULL,
    capacity     INTEGER NOT NULL CHECK (capacity >= 1),
    avoid_papers TEXT NOT NULL    -- JSON array
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS published (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    serial       INTEGER NOT NULL DEFAULT 0,
    revision     INTEGER NOT NULL,
    plan         TEXT NOT NULL,   -- JSON: {paper_id: [reviewer_id, reviewer_id]}
    explanations TEXT NOT NULL,   -- JSON: 每篇论文的分配与排除原因
    published_at TEXT NOT NULL
);
-- 评审人对当前发布版中分配关系的决定 (按发布序号 serial 隔离; 无行 = pending)
CREATE TABLE IF NOT EXISTS assignment_decisions (
    serial      INTEGER NOT NULL,
    paper_id    TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('confirmed', 'recused')),
    reason      TEXT,            -- 回避原因 (recused 时非空)
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (serial, paper_id, reviewer_id)
);
-- 评审人对"已确认且未回避"槽位提交的正式评分与评语 (按发布序号 serial 隔离;
-- 每个有效任务槽位至多一份; receipt 为首次提交时发放的收据, 相同内容重试原样返回。
-- receipt 不设全局唯一: 补位发布连续保留的确认槽位沿用同一份评语与同一收据,
-- 旧序号行保留供会务方追溯, 因此同一收据可随不同 serial 各存一行)
CREATE TABLE IF NOT EXISTS submitted_reviews (
    serial       INTEGER NOT NULL,
    paper_id     TEXT NOT NULL,
    reviewer_id  TEXT NOT NULL,
    score        INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    comment      TEXT NOT NULL,
    receipt      TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (serial, paper_id, reviewer_id)
);
-- 评审人一旦对某篇论文声明回避, 即成为后续所有分配 (含补位) 的硬回避
CREATE TABLE IF NOT EXISTS reviewer_recusals (
    reviewer_id      TEXT NOT NULL,
    paper_id         TEXT NOT NULL,
    reason           TEXT NOT NULL,
    created_serial   INTEGER NOT NULL,
    created_revision INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (reviewer_id, paper_id)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧版数据库补齐新增列 (SQLite 不支持 IF NOT EXISTS 的 ADD COLUMN)。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(published)").fetchall()}
    if "serial" not in cols:
        conn.execute("ALTER TABLE published ADD COLUMN serial INTEGER NOT NULL DEFAULT 0")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.path.abspath(DB_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('revision', '0')")
    return _conn


def get_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    return int(row["value"])


def bump_revision(conn: sqlite3.Connection) -> int:
    rev = get_revision(conn) + 1
    conn.execute("UPDATE meta SET value = ? WHERE key = 'revision'", (str(rev),))
    return rev


@contextmanager
def read_txn():
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


@contextmanager
def write_txn():
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def reset_for_tests() -> None:
    """关闭并丢弃当前连接 (仅测试使用, 配合重新设置 DB_PATH)。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
