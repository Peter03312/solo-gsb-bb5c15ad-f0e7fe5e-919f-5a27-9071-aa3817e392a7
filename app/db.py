"""SQLite 存储层：连接管理、表结构初始化、事务助手。

并发模型：
- 每个请求独立连接（WAL 模式，读写可并发）；
- 所有写操作包裹在 BEGIN IMMEDIATE 事务中，进入事务即取得写锁，
  保证"读取快照 -> 校验 -> 写入"是原子的；
- 发布版本时在同一个写事务内读取全部锚点与声段并落盘快照，
  因此每个版本必然对应一套完整、一致的锚点与声段快照。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anchors (
    device_id   TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, local_ms)
);

CREATE TABLE IF NOT EXISTS segments (
    capture_id      TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL REFERENCES devices(device_id),
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_device ON segments(device_id);

CREATE TABLE IF NOT EXISTS versions (
    version_no  INTEGER PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_anchors (
    version_no  INTEGER NOT NULL REFERENCES versions(version_no) ON DELETE CASCADE,
    device_id   TEXT NOT NULL,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    PRIMARY KEY (version_no, device_id, local_ms)
);

CREATE TABLE IF NOT EXISTS version_segments (
    version_no      INTEGER NOT NULL REFERENCES versions(version_no) ON DELETE CASCADE,
    capture_id      TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    status          TEXT NOT NULL,          -- 'located' | 'unlocatable'
    utc_start_ms    INTEGER,
    utc_end_ms      INTEGER,
    created_at      TEXT,
    PRIMARY KEY (version_no, capture_id)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str) -> None:
    """初始化数据库（建表）。服务启动时调用，幂等。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def session(db_path: str):
    """打开一个连接并在结束时关闭。"""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE，进入即持有写锁，读写同一快照。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_txn(conn: sqlite3.Connection):
    """读事务：保证多条 SELECT 看到同一快照。"""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
