"""SQLite 存储：表结构、连接与写事务。

所有变更都在 ``BEGIN IMMEDIATE`` 事务内完成：进入事务即取得写锁，
并发写被串行化，配合条件更新保证库存不会被超扣。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no TEXT NOT NULL UNIQUE,
    donor TEXT NOT NULL,
    source_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '登记',
    created_by TEXT NOT NULL,
    created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches (id),
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    usage_restriction TEXT NOT NULL DEFAULT '无限制',
    storage_location TEXT NOT NULL,
    qty_expected INTEGER NOT NULL CHECK (qty_expected > 0),
    qty_quarantined INTEGER NOT NULL DEFAULT 0 CHECK (qty_quarantined >= 0),
    qty_usable INTEGER NOT NULL DEFAULT 0 CHECK (qty_usable >= 0),
    qty_checked_out INTEGER NOT NULL DEFAULT 0 CHECK (qty_checked_out >= 0),
    qty_returned_donor INTEGER NOT NULL DEFAULT 0 CHECK (qty_returned_donor >= 0),
    qty_disposed INTEGER NOT NULL DEFAULT 0 CHECK (qty_disposed >= 0),
    created_ts TEXT NOT NULL,
    CHECK (qty_quarantined + qty_usable + qty_checked_out + qty_returned_donor + qty_disposed <= qty_expected)
);

CREATE TABLE IF NOT EXISTS acceptance_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items (id),
    check_item TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('合格', '不合格')),
    note TEXT NOT NULL DEFAULT '',
    inspector TEXT NOT NULL,
    created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items (id),
    borrower TEXT NOT NULL,
    used_by TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    qty_returned INTEGER NOT NULL DEFAULT 0 CHECK (qty_returned >= 0),
    qty_damaged INTEGER NOT NULL DEFAULT 0 CHECK (qty_damaged >= 0),
    status TEXT NOT NULL DEFAULT '在借',
    request_key TEXT,
    created_ts TEXT NOT NULL,
    CHECK (qty_returned + qty_damaged <= quantity)
);

CREATE TABLE IF NOT EXISTS request_keys (
    key TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    response TEXT NOT NULL,
    created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    batch_id INTEGER,
    item_id INTEGER,
    checkout_id INTEGER,
    quantity INTEGER,
    from_bucket TEXT,
    to_bucket TEXT,
    request_key TEXT,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_ledger_batch ON ledger_entries (batch_id);
CREATE INDEX IF NOT EXISTS idx_ledger_item ON ledger_entries (item_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开一个自动提交模式的连接，调用方负责关闭。"""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: str) -> None:
    """建库建表（幂等），必要时创建数据目录。"""
    path = Path(db_path)
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(path))
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` 写事务：进入即拿写锁，串行化所有变更。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
