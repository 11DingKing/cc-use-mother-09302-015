"""SQLite 连接与表结构。

只用 Python 标准库，零第三方依赖。通过 ``PRAGMA foreign_keys`` 与
``journal_mode=WAL`` 保证引用完整性与并发读写；数量一致性由应用层在
事务内加锁校验，另提供触发器作为负余额的最后防线。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS donors (
    donor_id     INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    contact      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id        INTEGER PRIMARY KEY,
    batch_no        TEXT NOT NULL UNIQUE,
    voucher_no      TEXT NOT NULL UNIQUE,          -- 捐赠凭证号（重复凭证直接拒绝）
    donor_id        INTEGER NOT NULL REFERENCES donors(donor_id),
    material_name   TEXT NOT NULL,
    category        TEXT NOT NULL,                 -- 如：服饰
    declared_qty    INTEGER NOT NULL CHECK (declared_qty > 0),
    use_restriction TEXT NOT NULL,                 -- 当前生效的用途限制
    registered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    received_qty    INTEGER NOT NULL DEFAULT 0,    -- 已实际接收数量（含部分接收）
    version         INTEGER NOT NULL DEFAULT 0     -- 批次行版本（并发控制）
);

CREATE TABLE IF NOT EXISTS batch_restrictions (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES batches(batch_id),
    seq           INTEGER NOT NULL,
    restriction   TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    changed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(batch_id, seq)
);

CREATE TABLE IF NOT EXISTS items (
    item_code   TEXT PRIMARY KEY,                 -- 每件物品的唯一编码
    batch_id    INTEGER NOT NULL REFERENCES batches(batch_id),
    seq_no      INTEGER NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',          -- 规格等描述
    UNIQUE(batch_id, seq_no)
);

CREATE TABLE IF NOT EXISTS inspections (
    id                INTEGER PRIMARY KEY,
    batch_id          INTEGER NOT NULL REFERENCES batches(batch_id),
    seq               INTEGER NOT NULL,            -- 批次第几次验收（可复验）
    decided_qty       INTEGER NOT NULL,            -- 本次决定数量
    decision          TEXT NOT NULL,               -- APPROVE / REJECT
    basis             TEXT NOT NULL DEFAULT '',    -- 验收依据/说明
    inspector         TEXT NOT NULL DEFAULT '',
    inspected_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(batch_id, seq)
);

CREATE TABLE IF NOT EXISTS inspection_checks (
    id            INTEGER PRIMARY KEY,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id),
    check_name    TEXT NOT NULL,                  -- 验收项目
    result        TEXT NOT NULL,                  -- PASS / FAIL / NA
    note          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS item_inspection_results (
    item_code     TEXT NOT NULL REFERENCES items(item_code),
    inspection_id INTEGER NOT NULL REFERENCES inspections(id),
    accepted      INTEGER NOT NULL,               -- 0/1：是否批准放行
    PRIMARY KEY (item_code, inspection_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    txn_type    TEXT NOT NULL,
    ref_no      TEXT UNIQUE,                       -- 外部业务单号（接收单/领用单号…），幂等防重
    batch_id    INTEGER REFERENCES batches(batch_id),
    item_code   TEXT REFERENCES items(item_code),
    summary     TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 双分录台账：同一事务的分录增量之和必须为 0
CREATE TABLE IF NOT EXISTS ledger_entries (
    id          INTEGER PRIMARY KEY,
    txn_id      INTEGER NOT NULL REFERENCES transactions(id),
    account     TEXT NOT NULL,
    item_code   TEXT REFERENCES items(item_code),
    delta       INTEGER NOT NULL,                  -- 正入负出
    CHECK (delta <> 0)
);

CREATE INDEX IF NOT EXISTS idx_entries_item ON ledger_entries(item_code, account);
CREATE INDEX IF NOT EXISTS idx_entries_txn ON ledger_entries(txn_id);

-- 物品级去向事件：每件物品全生命周期的有序记录
CREATE TABLE IF NOT EXISTS item_trace (
    id          INTEGER PRIMARY KEY,
    item_code   TEXT NOT NULL REFERENCES items(item_code),
    txn_id      INTEGER NOT NULL REFERENCES transactions(id),
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trace_item ON item_trace(item_code, id);

-- 领用在外的物品清单（每件一条），并发领用靠对可用物品行加锁竞争
CREATE TABLE IF NOT EXISTS active_issues (
    item_code   TEXT PRIMARY KEY REFERENCES items(item_code),
    txn_id      INTEGER NOT NULL REFERENCES transactions(id),
    teacher     TEXT NOT NULL,
    issued_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 实物账户（非对冲账户）在物品维度上不允许负余额的最后防线
CREATE TRIGGER IF NOT EXISTS trg_item_balance_nonnegative
AFTER INSERT ON ledger_entries
WHEN NEW.item_code IS NOT NULL
     AND NEW.account != '捐赠来源'
BEGIN
    SELECT CASE
        WHEN (
            SELECT COALESCE(SUM(delta), 0) FROM ledger_entries
            WHERE item_code = NEW.item_code AND account = NEW.account
        ) < 0
    THEN RAISE(ABORT, '实物账户余额不能为负')
    END;
END;
"""


def connect(database: str | Path = ":memory:") -> sqlite3.Connection:
    """打开数据库连接并完成连接级设置。"""
    conn = sqlite3.connect(database, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    """建表（幂等）。"""
    conn.executescript(SCHEMA)
