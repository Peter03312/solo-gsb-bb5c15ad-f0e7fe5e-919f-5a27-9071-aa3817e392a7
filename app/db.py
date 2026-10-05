"""SQLite 存储层：连接管理、表结构初始化、事务助手。

周期模型（时钟周期）：
- 每台设备可登记若干"时钟周期"（cycle），周期在设备内唯一；设备本地毫秒
  （如开机计时）重启后可归零，不同周期可复用相同的 local_ms；
- 锚点与声段都归属于某个 (device_id, cycle_id)；缺省周期固定为 "default"，
  老数据（升级前已存在的行）一律视为 default 周期；
- 锚点递增校验与线性漂移映射均按周期独立执行，声段只用所属周期的锚点投影；
- 发布版本时在同一写事务内把各周期的锚点、声段投影整体冻结。

并发模型：
- 每个请求独立连接（WAL 模式，读写可并发）；
- 所有写操作包裹在 BEGIN IMMEDIATE 事务中，进入事务即取得写锁，
  保证"读取快照 -> 校验 -> 写入"是原子的；
- 发布版本时在同一个写事务内读取全部锚点与声段并落盘快照，
  因此每个版本必然对应一套完整、一致的各周期锚点 + 声段快照。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DEFAULT_CYCLE_ID = "default"

# ---------------------------------------------------------------- 表结构

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    device_id   TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    cycle_id    TEXT NOT NULL,
    name        TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, cycle_id)
);

CREATE TABLE IF NOT EXISTS anchors (
    device_id   TEXT NOT NULL,
    cycle_id    TEXT NOT NULL DEFAULT 'default',
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, cycle_id, local_ms),
    FOREIGN KEY (device_id, cycle_id) REFERENCES cycles(device_id, cycle_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS segments (
    capture_id      TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    cycle_id        TEXT NOT NULL DEFAULT 'default',
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (device_id, cycle_id) REFERENCES cycles(device_id, cycle_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_segments_device ON segments(device_id);
-- idx_segments_cycle 在迁移后创建（旧库 segments 需先 ALTER 出 cycle_id 列）

CREATE TABLE IF NOT EXISTS versions (
    version_no  INTEGER PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_cycles (
    version_no  INTEGER NOT NULL REFERENCES versions(version_no) ON DELETE CASCADE,
    device_id   TEXT NOT NULL,
    cycle_id    TEXT NOT NULL,
    name        TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (version_no, device_id, cycle_id)
);

CREATE TABLE IF NOT EXISTS version_anchors (
    version_no  INTEGER NOT NULL REFERENCES versions(version_no) ON DELETE CASCADE,
    device_id   TEXT NOT NULL,
    cycle_id    TEXT NOT NULL DEFAULT 'default',
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    PRIMARY KEY (version_no, device_id, cycle_id, local_ms)
);

CREATE TABLE IF NOT EXISTS version_segments (
    version_no      INTEGER NOT NULL REFERENCES versions(version_no) ON DELETE CASCADE,
    capture_id      TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    cycle_id        TEXT NOT NULL DEFAULT 'default',
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

# 旧库（周期功能上线前）的 anchors 表主键为 (device_id, local_ms)，
# 需要重建为 (device_id, cycle_id, local_ms)；旧行全部归入 default 周期。
_REBUILD_ANCHORS_STMTS = [
    """
    CREATE TABLE anchors_new (
        device_id   TEXT NOT NULL,
        cycle_id    TEXT NOT NULL DEFAULT 'default',
        local_ms    INTEGER NOT NULL,
        utc_ms      INTEGER NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (device_id, cycle_id, local_ms),
        FOREIGN KEY (device_id, cycle_id) REFERENCES cycles(device_id, cycle_id) ON DELETE CASCADE
    )
    """,
    """
    INSERT INTO anchors_new(device_id, cycle_id, local_ms, utc_ms, created_at)
    SELECT device_id, 'default', local_ms, utc_ms, created_at FROM anchors
    """,
    "DROP TABLE anchors",
    "ALTER TABLE anchors_new RENAME TO anchors",
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str) -> None:
    """初始化数据库（建表 + 旧库迁移）。服务启动时调用，幂等。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        # CREATE TABLE IF NOT EXISTS 本身幂等；SQLite DDL 支持事务，
        # 建表与随后的旧库迁移放进同一个写事务，避免升级中途出现半迁移状态。
        # 注意：executescript 会隐式提交，故这里逐条 execute。
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split_statements(SCHEMA):
                conn.execute(stmt)
            _migrate(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _split_statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


def _migrate(conn: sqlite3.Connection) -> None:
    """把周期功能上线前的旧库升级到周期模型（幂等）。

    - cycles 表：为每台已注册设备补登记 default 周期；
    - anchors 表：旧主键不含 cycle_id，需重建，旧行归入 default；
    - segments / version_anchors / version_segments：仅补 cycle_id 列，
      旧值全部为 default；已发布旧版本的查询结果因此保持不变。
    """
    legacy_anchors = _table_exists(conn, "anchors") and \
        "cycle_id" not in _columns(conn, "anchors")
    if legacy_anchors:
        # 必须先补齐 cycles.default，外键才成立
        conn.execute(
            "INSERT OR IGNORE INTO cycles(device_id, cycle_id, name, created_at) "
            "SELECT device_id, 'default', NULL, created_at FROM devices")
        for stmt in _REBUILD_ANCHORS_STMTS:
            conn.execute(stmt)
        conn.execute("ALTER TABLE segments ADD COLUMN cycle_id TEXT NOT NULL DEFAULT 'default'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_cycle ON segments(device_id, cycle_id)")

    if _table_exists(conn, "version_anchors") and \
            "cycle_id" not in _columns(conn, "version_anchors"):
        conn.execute(
            "ALTER TABLE version_anchors ADD COLUMN cycle_id TEXT NOT NULL DEFAULT 'default'")

    if _table_exists(conn, "version_segments") and \
            "cycle_id" not in _columns(conn, "version_segments"):
        conn.execute(
            "ALTER TABLE version_segments ADD COLUMN cycle_id TEXT NOT NULL DEFAULT 'default'")

    # 各表结构就位后（新库建表 / 旧库 ALTER 补列）再建周期索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_cycle "
                 "ON segments(device_id, cycle_id)")


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
