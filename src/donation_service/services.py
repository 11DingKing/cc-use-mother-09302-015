"""领域服务：双分录台账与隔离业务操作。

核心机制
========

* 每个写操作都在 ``BEGIN IMMEDIATE`` 事务内完成：SQLite 将写事务串行化，
  因此“并发领用”同一物品时后到者会在锁内重新读到最新余额并以 409 拒绝，
  不会出现超发。
* 每笔事务生成若干 ``ledger_entries``，全部分录增量之和为 0；实物账户
  （非“捐赠来源”）在任何物品维度上不允许为负，过账前逐户投影校验，
  数据库触发器是最后防线。
* 外部业务单号（接收单、领用单号等）写入 ``transactions.ref_no`` 的唯一键，
  重复提交整笔拒绝，实现幂等防重；捐赠凭证号在批次表唯一。
* 每件物品在 ``item_trace`` 留下有序事件，配合台账余额即可从捐赠入口
  追踪到领用、归还或处置结果。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from .accounts import (
    Accounts,
    TxnType,
)
from .errors import (
    DomainError,
    DuplicateBatchError,
    DuplicateItemError,
    DuplicateReferenceError,
    DuplicateVoucherError,
    InsufficientBalanceError,
    NotFoundError,
    QuarantineControlError,
    ValidationError,
)

# item_trace 动作文案
ACT_REGISTER = "登记"
ACT_RECEIVE = "接收"
ACT_APPROVE = "验收批准"
ACT_REJECT = "验收拒收"
ACT_RELEASE = "放行"
ACT_RESTRICTION = "限制变更"
ACT_RETURN = "退回捐赠方"
ACT_DISPOSE = "处置核销"
ACT_ISSUE = "领用"
ACT_GIVE_BACK = "归还入库"
ACT_GIVE_BACK_DISPOSE = "归还并处置"


# 台账账户 → 对外展示状态
_STATE_BY_ACCOUNT = {
    Accounts.PENDING: "登记",
    Accounts.QUARANTINE: "隔离",
    Accounts.REJECTED: "验收拒收",
    Accounts.APPROVED: "已批准待放行",
    Accounts.AVAILABLE: "可用",
    Accounts.ISSUED: "领用",
    Accounts.RETURNED: "退回",
    Accounts.DISPOSED: "处置",
}


def _state_of(balances: dict[str, int]) -> str:
    """根据物品台账余额推导当前状态（每件物品任一时刻只在一个实物账户）。"""
    held = [account for account in Accounts.PHYSICAL if balances.get(account, 0) > 0]
    if not held:
        return "未知"
    if len(held) > 1:
        # 双分录记账下每件物品恒为 1 件且只在一个实物账户；出现多个即数据损坏
        return "状态异常：" + "、".join(held)
    return _STATE_BY_ACCOUNT[held[0]]


def _integrity_error(exc: sqlite3.IntegrityError) -> DomainError:
    msg = str(exc)
    if "batches.voucher_no" in msg:
        return DuplicateVoucherError("捐赠凭证号已存在，禁止重复登记")
    if "batches.batch_no" in msg:
        return DuplicateBatchError("批次编号已存在")
    if "items.item_code" in msg or "items.batch_id, items.seq_no" in msg:
        return DuplicateItemError("物品编码或批次内序号重复")
    if "transactions.ref_no" in msg:
        return DuplicateReferenceError("业务单号已使用，禁止重复入账")
    if "active_issues" in msg:
        return InsufficientBalanceError("物品已被其他教师领用")
    return ValidationError(f"数据约束失败：{msg}")


class Ledger:
    """台账读写仓储，所有方法须在事务上下文内调用。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---------- 基础查询 ----------

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def donor_by_name(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM donors WHERE name = ?", (name,)).fetchone()

    def donor_by_id(self, donor_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM donors WHERE donor_id = ?", (donor_id,)).fetchone()

    def batch(self, *, batch_id: int | None = None, batch_no: str | None = None) -> sqlite3.Row | None:
        if batch_id is not None:
            return self.conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        return self.conn.execute("SELECT * FROM batches WHERE batch_no = ?", (batch_no,)).fetchone()

    def require_batch(self, *, batch_id: int | None = None, batch_no: str | None = None) -> sqlite3.Row:
        row = self.batch(batch_id=batch_id, batch_no=batch_no)
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def item(self, item_code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE item_code = ?", (item_code,)).fetchone()

    def require_item(self, item_code: str) -> sqlite3.Row:
        row = self.item(item_code)
        if row is None:
            raise NotFoundError(f"物品不存在：{item_code}")
        return row

    def batch_items(self, batch_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM items WHERE batch_id = ? ORDER BY seq_no", (batch_id,)
            )
        )

    def item_balance(self, item_code: str, account: str) -> int:
        return self.scalar(
            "SELECT COALESCE(SUM(delta),0) FROM ledger_entries WHERE item_code=? AND account=?",
            (item_code, account),
        )

    def item_balances(self, item_code: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT account, SUM(delta) AS qty FROM ledger_entries WHERE item_code=? GROUP BY account",
            (item_code,),
        )
        return {r["account"]: r["qty"] for r in rows}

    def batch_balances(self, batch_id: int) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT e.account, SUM(e.delta) AS qty
            FROM ledger_entries e
            JOIN items i ON i.item_code = e.item_code
            WHERE i.batch_id = ?
            GROUP BY e.account
            HAVING qty != 0
            """,
            (batch_id,),
        )
        return {r["account"]: r["qty"] for r in rows}

    def items_in_state(self, batch_id: int, account: str) -> list[str]:
        """当前处于某实物账户的批次内物品，按序号排序。"""
        rows = self.conn.execute(
            """
            SELECT i.item_code
            FROM items i
            JOIN ledger_entries e ON e.item_code = i.item_code
            WHERE i.batch_id = ? AND e.account = ?
            GROUP BY i.item_code
            HAVING SUM(e.delta) > 0
            ORDER BY i.seq_no
            """,
            (batch_id, account),
        )
        return [r["item_code"] for r in rows]

    def current_location(self, item_code: str) -> str:
        row = self.conn.execute(
            """
            SELECT location FROM item_trace
            WHERE item_code = ? AND location != ''
            ORDER BY id DESC LIMIT 1
            """,
            (item_code,),
        ).fetchone()
        return "" if row is None else row["location"]

    # ---------- 过账 ----------

    def post_txn(
        self,
        *,
        txn_type: str,
        entries: list[tuple[str, str, int]],
        traces: list[dict[str, Any]] | None = None,
        batch_id: int | None = None,
        item_code: str | None = None,
        ref_no: str | None = None,
        summary: str = "",
        created_by: str = "",
    ) -> int:
        """过一笔事务：先投影校验余额，再写事务、分录与去向事件（原子完成）。

        允许 ``entries`` 为空——用途限制变更不移动实物，仅留事务与去向事件。
        """
        if entries:
            if sum(delta for _, _, delta in entries) != 0:
                raise ValidationError("分录借贷不平衡")

            # 实物账户余额投影：任何物品维度不得为负
            projected: dict[tuple[str, str], int] = defaultdict(int)
            for account, ic, delta in entries:
                if account != Accounts.SOURCE:
                    projected[(ic, account)] += delta
            for (ic, account), change in projected.items():
                if change < 0 and self.item_balance(ic, account) + change < 0:
                    raise InsufficientBalanceError(
                        f"物品 {ic} 的「{account}」余额不足，操作被拒绝"
                    )

        try:
            cur = self.conn.execute(
                """
                INSERT INTO transactions (txn_type, ref_no, batch_id, item_code, summary, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (txn_type, ref_no, batch_id, item_code, summary, created_by),
            )
            txn_id = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT INTO ledger_entries (txn_id, account, item_code, delta) VALUES (?, ?, ?, ?)",
                [(txn_id, account, ic, delta) for account, ic, delta in entries],
            )
            for trace in traces or []:
                self.conn.execute(
                    """
                    INSERT INTO item_trace (item_code, txn_id, action, actor, location, note)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace["item_code"],
                        txn_id,
                        trace["action"],
                        trace.get("actor", created_by),
                        trace.get("location", ""),
                        trace.get("note", summary),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise _integrity_error(exc) from exc
        return txn_id


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数")
    return value


class Service:
    """对外领域服务。每个方法对应一个业务用例，自成一个事务。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ================= 捐赠方 =================

    def register_donor(self, name: str, contact: str = "") -> dict[str, Any]:
        if not name or not str(name).strip():
            raise ValidationError("捐赠方名称不能为空")
        with Ledger(self.conn).transaction():
            existing = self.conn.execute(
                "SELECT * FROM donors WHERE name = ?", (name,)
            ).fetchone()
            if existing:
                raise ValidationError("捐赠方已存在")
            cur = self.conn.execute(
                "INSERT INTO donors (name, contact) VALUES (?, ?)", (name, contact)
            )
            donor_id = int(cur.lastrowid)
        return {"donor_id": donor_id, "name": name, "contact": contact}

    def list_donors(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM donors ORDER BY donor_id").fetchall()
        return [dict(r) for r in rows]

    # ================= 批次与逐件登记 =================

    def register_batch(
        self,
        *,
        batch_no: str,
        voucher_no: str,
        material_name: str,
        declared_qty: int,
        use_restriction: str,
        donor_id: int | None = None,
        donor_name: str | None = None,
        category: str = "",
        items: Iterable[Any] | None = None,
        registered_by: str = "",
    ) -> dict[str, Any]:
        declared_qty = _as_int(declared_qty, "declared_qty")
        if declared_qty <= 0:
            raise ValidationError("申报数量必须为正")
        for label, value in (
            ("batch_no", batch_no),
            ("voucher_no", voucher_no),
            ("material_name", material_name),
            ("use_restriction", use_restriction),
        ):
            if not value or not str(value).strip():
                raise ValidationError(f"{label} 不能为空")

        # 规范化每件登记内容
        normalized: list[dict[str, str]] = []
        for seq, raw in enumerate(items or [], start=1):
            if isinstance(raw, str):
                normalized.append({"item_code": raw, "detail": ""})
            elif isinstance(raw, dict):
                normalized.append(
                    {
                        "item_code": str(raw.get("item_code") or "").strip(),
                        "detail": str(raw.get("detail") or ""),
                    }
                )
            else:
                raise ValidationError("items 元素必须是编码字符串或对象")
        if not normalized:
            normalized = [{"item_code": "", "detail": ""} for _ in range(declared_qty)]
        if len(normalized) != declared_qty:
            raise ValidationError(
                f"逐件登记数量 {len(normalized)} 与申报数量 {declared_qty} 不一致"
            )
        for idx, row in enumerate(normalized, start=1):
            if not row["item_code"]:
                row["item_code"] = f"{batch_no}-{idx:03d}"
        if len({r["item_code"] for r in normalized}) != declared_qty:
            raise DuplicateItemError("批次内物品编码重复")

        ledger = Ledger(self.conn)
        with ledger.transaction():
            if donor_id is None:
                if not donor_name:
                    raise ValidationError("必须提供 donor_id 或 donor_name")
                donor = ledger.donor_by_name(donor_name)
                if donor is None:
                    cur = self.conn.execute(
                        "INSERT INTO donors (name) VALUES (?)", (donor_name,)
                    )
                    donor_id = int(cur.lastrowid)
                else:
                    donor_id = int(donor["donor_id"])
            elif ledger.donor_by_id(donor_id) is None:
                raise NotFoundError("捐赠方不存在")

            try:
                cur = self.conn.execute(
                    """
                    INSERT INTO batches (batch_no, voucher_no, donor_id, material_name,
                                         category, declared_qty, use_restriction)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_no,
                        voucher_no,
                        donor_id,
                        material_name,
                        category,
                        declared_qty,
                        use_restriction,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise _integrity_error(exc) from exc
            batch_id = int(cur.lastrowid)

            self.conn.executemany(
                "INSERT INTO items (item_code, batch_id, seq_no, detail) VALUES (?, ?, ?, ?)",
                [(r["item_code"], batch_id, seq, r["detail"]) for seq, r in enumerate(normalized, start=1)],
            )
            self.conn.execute(
                "INSERT INTO batch_restrictions (batch_id, seq, restriction, reason) VALUES (?, 1, ?, '登记时录入')",
                (batch_id, use_restriction),
            )

            entries: list[tuple[str, str, int]] = []
            traces: list[dict[str, Any]] = []
            for row in normalized:
                ic = row["item_code"]
                entries.append((Accounts.SOURCE, ic, -1))
                entries.append((Accounts.PENDING, ic, 1))
                traces.append(
                    {
                        "item_code": ic,
                        "action": ACT_REGISTER,
                        "actor": registered_by,
                        "note": f"凭证 {voucher_no}；用途限制：{use_restriction}",
                    }
                )
            ledger.post_txn(
                txn_type=TxnType.REGISTER,
                batch_id=batch_id,
                summary=f"登记批次 {batch_no}（{material_name} × {declared_qty}）",
                created_by=registered_by,
                entries=entries,
                traces=traces,
            )
        return self.get_batch(batch_no=batch_no)

    # ================= 接收（支持部分接收） =================

    def receive_items(
        self,
        *,
        batch_no: str,
        item_codes: list[str] | None = None,
        qty: int | None = None,
        ref_no: str,
        location: str,
        received_by: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        if not ref_no:
            raise ValidationError("接收必须提供业务单号 ref_no")
        if not location:
            raise ValidationError("接收必须登记保管位置")
        ledger = Ledger(self.conn)
        with ledger.transaction():
            batch = ledger.require_batch(batch_no=batch_no)
            batch_id = int(batch["batch_id"])
            pending = ledger.items_in_state(batch_id, Accounts.PENDING)
            targets = self._select_targets(pending, item_codes, qty, batch_id, ledger)

            entries: list[tuple[str, str, int]] = []
            traces = [
                {
                    "item_code": ic,
                    "action": ACT_RECEIVE,
                    "actor": received_by,
                    "location": location,
                    "note": note or f"接收入隔离区：{location}",
                }
                for ic in targets
            ]
            for ic in targets:
                entries.append((Accounts.PENDING, ic, -1))
                entries.append((Accounts.QUARANTINE, ic, 1))
            ledger.post_txn(
                txn_type=TxnType.RECEIVE,
                ref_no=ref_no,
                batch_id=batch_id,
                summary=f"接收 {len(targets)} 件入隔离（{location}）",
                created_by=received_by,
                entries=entries,
                traces=traces,
            )
            self._refresh_received_qty(ledger, batch_id)
        return self.get_batch(batch_no=batch_no)

    # ================= 验收（验收项目 + 逐件批准决定） =================

    def inspect_batch(
        self,
        *,
        batch_no: str,
        decisions: dict[str, bool] | None = None,
        qty_approved: int | None = None,
        checks: list[dict[str, str]] | None = None,
        basis: str = "",
        inspector: str = "",
    ) -> dict[str, Any]:
        """登记验收。

        ``decisions``：{物品编码: 是否批准}；或给 ``qty_approved`` 表示按序号
        批准前 N 件、其余拒收。每件必须当前在隔离库存（或复验拒收件）。
        ``checks``：[{check_name, result: PASS/FAIL/NA, note}] 验收项目记录。
        """
        ledger = Ledger(self.conn)
        with ledger.transaction():
            batch = ledger.require_batch(batch_no=batch_no)
            batch_id = int(batch["batch_id"])
            in_quarantine = set(ledger.items_in_state(batch_id, Accounts.QUARANTINE))
            in_rejected = set(ledger.items_in_state(batch_id, Accounts.REJECTED))
            decidable = in_quarantine | in_rejected
            if not decidable:
                raise QuarantineControlError("该批次没有待验收（隔离中）的物品")

            if decisions is None:
                if qty_approved is None:
                    raise ValidationError("必须提供 decisions 或 qty_approved")
                qty_approved = _as_int(qty_approved, "qty_approved")
                ordered = sorted(decidable, key=self._seq_key(ledger))
                if not 0 <= qty_approved <= len(ordered):
                    raise ValidationError("批准数量超出待验收范围")
                decision_map = {ic: idx < qty_approved for idx, ic in enumerate(ordered)}
            else:
                if not isinstance(decisions, dict) or not decisions:
                    raise ValidationError("decisions 必须是非空映射")
                unknown = set(decisions) - decidable
                if unknown:
                    raise QuarantineControlError(
                        "以下物品不在待验收状态，不能验收：" + "、".join(sorted(unknown))
                    )
                decision_map = {str(k): bool(v) for k, v in decisions.items()}

            for check in checks or []:
                result = str(check.get("result", "")).upper()
                if result not in ("PASS", "FAIL", "NA"):
                    raise ValidationError(f"验收项结论非法：{check.get('check_name')}")
                if not str(check.get("check_name", "")).strip():
                    raise ValidationError("验收项目名称不能为空")

            seq = int(
                ledger.scalar(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM inspections WHERE batch_id=?",
                    (batch_id,),
                )
            )
            cur = self.conn.execute(
                """
                INSERT INTO inspections (batch_id, seq, decided_qty, decision, basis, inspector)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    seq,
                    len(decision_map),
                    "MIXED" if len({*decision_map.values()}) > 1 else (
                        "APPROVE" if all(decision_map.values()) else "REJECT"
                    ),
                    basis,
                    inspector,
                ),
            )
            inspection_id = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT INTO inspection_checks (inspection_id, check_name, result, note) VALUES (?, ?, ?, ?)",
                [
                    (
                        inspection_id,
                        str(c["check_name"]),
                        str(c["result"]).upper(),
                        str(c.get("note", "")),
                    )
                    for c in checks or []
                ],
            )
            self.conn.executemany(
                "INSERT INTO item_inspection_results (item_code, inspection_id, accepted) VALUES (?, ?, ?)",
                [(ic, inspection_id, 1 if ok else 0) for ic, ok in decision_map.items()],
            )

            entries: list[tuple[str, str, int]] = []
            traces: list[dict[str, Any]] = []
            for ic, accepted in decision_map.items():
                source = Accounts.REJECTED if ic in in_rejected else Accounts.QUARANTINE
                target = Accounts.APPROVED if accepted else Accounts.REJECTED
                entries.append((source, ic, -1))
                entries.append((target, ic, 1))
                traces.append(
                    {
                        "item_code": ic,
                        "action": ACT_APPROVE if accepted else ACT_REJECT,
                        "actor": inspector,
                        "note": f"第 {seq} 次验收：{'批准放行' if accepted else '拒收'}",
                    }
                )
            ledger.post_txn(
                txn_type=TxnType.INSPECTION,
                batch_id=batch_id,
                item_code=None,
                summary=f"第 {seq} 次验收，决定 {len(decision_map)} 件",
                created_by=inspector,
                entries=entries,
                traces=traces,
            )
        return self.get_batch(batch_no=batch_no)

    # ================= 放行（仅批准数量可转入可用库存） =================

    def release_items(
        self,
        *,
        batch_no: str,
        item_codes: list[str] | None = None,
        qty: int | None = None,
        ref_no: str | None = None,
        location: str,
        released_by: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        if not location:
            raise ValidationError("放行必须登记入库位置")
        ledger = Ledger(self.conn)
        with ledger.transaction():
            batch = ledger.require_batch(batch_no=batch_no)
            batch_id = int(batch["batch_id"])
            approved = ledger.items_in_state(batch_id, Accounts.APPROVED)
            targets = self._select_targets(approved, item_codes, qty, batch_id, ledger)

            entries: list[tuple[str, str, int]] = []
            traces = [
                {
                    "item_code": ic,
                    "action": ACT_RELEASE,
                    "actor": released_by,
                    "location": location,
                    "note": note or f"批准放行转可用库存：{location}",
                }
                for ic in targets
            ]
            for ic in targets:
                entries.append((Accounts.APPROVED, ic, -1))
                entries.append((Accounts.AVAILABLE, ic, 1))
            ledger.post_txn(
                txn_type=TxnType.RELEASE,
                ref_no=ref_no,
                batch_id=batch_id,
                summary=f"放行 {len(targets)} 件转可用库存",
                created_by=released_by,
                entries=entries,
                traces=traces,
            )
        return self.get_batch(batch_no=batch_no)

    # ================= 用途限制变更 =================

    def change_restriction(
        self,
        *,
        batch_no: str,
        restriction: str,
        reason: str,
        changed_by: str = "",
    ) -> dict[str, Any]:
        if not restriction or not str(restriction).strip():
            raise ValidationError("新用途限制不能为空")
        if not reason or not str(reason).strip():
            raise ValidationError("限制变更必须填写原因")
        ledger = Ledger(self.conn)
        with ledger.transaction():
            batch = ledger.require_batch(batch_no=batch_no)
            batch_id = int(batch["batch_id"])
            seq = int(
                ledger.scalar(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM batch_restrictions WHERE batch_id=?",
                    (batch_id,),
                )
            )
            self.conn.execute(
                "INSERT INTO batch_restrictions (batch_id, seq, restriction, reason) VALUES (?, ?, ?, ?)",
                (batch_id, seq, restriction, reason),
            )
            self.conn.execute(
                "UPDATE batches SET use_restriction=?, version=version+1 WHERE batch_id=?",
                (restriction, batch_id),
            )
            item_codes = [r["item_code"] for r in ledger.batch_items(batch_id)]
            traces = [
                {
                    "item_code": ic,
                    "action": ACT_RESTRICTION,
                    "actor": changed_by,
                    "note": f"限制变更为：{restriction}（原因：{reason}）",
                }
                for ic in item_codes
            ]
            ledger.post_txn(
                txn_type=TxnType.RESTRICTION_CHANGE,
                batch_id=batch_id,
                summary=f"用途限制变更：{restriction}",
                created_by=changed_by,
                entries=[],
                traces=traces,
            )
        return self.get_batch(batch_no=batch_no)

    # ================= 退回 / 处置 =================

    def return_items(
        self,
        *,
        batch_no: str,
        item_codes: list[str],
        reason: str,
        ref_no: str | None = None,
        handled_by: str = "",
    ) -> dict[str, Any]:
        return self._exit_items(
            batch_no=batch_no,
            item_codes=item_codes,
            reason=reason,
            allowed_from=(Accounts.QUARANTINE, Accounts.REJECTED),
            dest=Accounts.RETURNED,
            txn_type=TxnType.RETURN,
            action=ACT_RETURN,
            ref_no=ref_no,
            handled_by=handled_by,
        )

    def dispose_items(
        self,
        *,
        batch_no: str,
        item_codes: list[str],
        reason: str,
        method: str = "",
        ref_no: str | None = None,
        handled_by: str = "",
    ) -> dict[str, Any]:
        if not method:
            raise ValidationError("处置必须登记处置方式")
        return self._exit_items(
            batch_no=batch_no,
            item_codes=item_codes,
            reason=f"{reason}；处置方式：{method}",
            allowed_from=(Accounts.QUARANTINE, Accounts.REJECTED, Accounts.AVAILABLE),
            dest=Accounts.DISPOSED,
            txn_type=TxnType.DISPOSE,
            action=ACT_DISPOSE,
            ref_no=ref_no,
            handled_by=handled_by,
        )

    def _exit_items(
        self,
        *,
        batch_no: str,
        item_codes: list[str],
        reason: str,
        allowed_from: tuple[str, ...],
        dest: str,
        txn_type: str,
        action: str,
        ref_no: str | None,
        handled_by: str,
    ) -> dict[str, Any]:
        if not item_codes:
            raise ValidationError("必须指定物品")
        if not reason:
            raise ValidationError("必须填写原因")
        ledger = Ledger(self.conn)
        with ledger.transaction():
            batch = ledger.require_batch(batch_no=batch_no)
            batch_id = int(batch["batch_id"])
            entries: list[tuple[str, str, int]] = []
            traces: list[dict[str, Any]] = []
            for ic in item_codes:
                item = ledger.require_item(ic)
                if int(item["batch_id"]) != batch_id:
                    raise ValidationError(f"物品 {ic} 不属于批次 {batch_no}")
                balances = ledger.item_balances(ic)
                source = next((a for a in allowed_from if balances.get(a, 0) > 0), None)
                if source is None:
                    raise QuarantineControlError(
                        f"物品 {ic} 当前状态不允许此操作（未经验收批准不得放行，在途/已结物品不可退回）"
                    )
                entries.append((source, ic, -1))
                entries.append((dest, ic, 1))
                traces.append(
                    {"item_code": ic, "action": action, "actor": handled_by, "note": reason}
                )
            ledger.post_txn(
                txn_type=txn_type,
                ref_no=ref_no,
                batch_id=batch_id,
                summary=f"{action} {len(item_codes)} 件：{reason}",
                created_by=handled_by,
                entries=entries,
                traces=traces,
            )
            self._refresh_received_qty(ledger, batch_id)
        return self.get_batch(batch_no=batch_no)

    # ================= 领用 / 归还（并发安全） =================

    def issue_items(
        self,
        *,
        teacher: str,
        item_codes: list[str] | None = None,
        qty: int | None = None,
        batch_no: str | None = None,
        ref_no: str,
        purpose: str = "",
    ) -> dict[str, Any]:
        if not teacher:
            raise ValidationError("领用必须登记教师")
        if not ref_no:
            raise ValidationError("领用必须提供领用单号 ref_no")
        ledger = Ledger(self.conn)
        with ledger.transaction():
            batch_id: int | None = None
            if batch_no is not None:
                batch_id = int(ledger.require_batch(batch_no=batch_no)["batch_id"])
            if item_codes is None:
                if qty is None:
                    raise ValidationError("必须提供 item_codes 或 qty")
                qty = _as_int(qty, "qty")
                if qty <= 0:
                    raise ValidationError("领用数量必须为正")
                if batch_id is None:
                    raise ValidationError("按数量领用必须指定 batch_no")
                candidates = ledger.items_in_state(batch_id, Accounts.AVAILABLE)
                targets = candidates[:qty]
                if len(targets) < qty:
                    raise InsufficientBalanceError(
                        f"可用库存不足：需要 {qty} 件，仅剩 {len(candidates)} 件"
                    )
            else:
                if not item_codes:
                    raise ValidationError("item_codes 不能为空")
                if qty is not None:
                    raise ValidationError("item_codes 与 qty 只能二选一")
                targets = list(item_codes)
                for ic in targets:
                    item = ledger.require_item(ic)
                    if batch_id is not None and int(item["batch_id"]) != batch_id:
                        raise ValidationError(f"物品 {ic} 不属于批次 {batch_no}")
                    if ledger.item_balance(ic, Accounts.AVAILABLE) <= 0:
                        raise QuarantineControlError(
                            f"物品 {ic} 不在可用库存：未完成验收放行的隔离物品禁止领用"
                        )

            entries: list[tuple[str, str, int]] = []
            traces: list[dict[str, Any]] = []
            for ic in targets:
                entries.append((Accounts.AVAILABLE, ic, -1))
                entries.append((Accounts.ISSUED, ic, 1))
                traces.append(
                    {
                        "item_code": ic,
                        "action": ACT_ISSUE,
                        "actor": teacher,
                        "location": f"教师领用：{teacher}",
                        "note": f"领用单 {ref_no}" + (f"；用途：{purpose}" if purpose else ""),
                    }
                )
            txn_id = ledger.post_txn(
                txn_type=TxnType.ISSUE,
                ref_no=ref_no,
                batch_id=batch_id,
                summary=f"{teacher} 领用 {len(targets)} 件",
                created_by=teacher,
                entries=entries,
                traces=traces,
            )
            try:
                self.conn.executemany(
                    "INSERT INTO active_issues (item_code, txn_id, teacher) VALUES (?, ?, ?)",
                    [(ic, txn_id, teacher) for ic in targets],
                )
            except sqlite3.IntegrityError as exc:
                raise _integrity_error(exc) from exc
        return {"ref_no": ref_no, "teacher": teacher, "item_codes": targets}

    def return_issued_items(
        self,
        *,
        teacher: str,
        item_codes: list[str],
        outcome: str = "REUSE",
        reason: str = "",
        handled_by: str = "",
    ) -> dict[str, Any]:
        """教师归还。``outcome=REUSE`` 重回可用库存；``DISCARD`` 则处置核销。"""
        if outcome not in ("REUSE", "DISCARD"):
            raise ValidationError("outcome 必须是 REUSE 或 DISCARD")
        if not item_codes:
            raise ValidationError("必须归还至少一件物品")
        ledger = Ledger(self.conn)
        with ledger.transaction():
            entries: list[tuple[str, str, int]] = []
            traces: list[dict[str, Any]] = []
            batch_ids: set[int] = set()
            for ic in item_codes:
                item = ledger.require_item(ic)
                batch_ids.add(int(item["batch_id"]))
                row = self.conn.execute(
                    "SELECT * FROM active_issues WHERE item_code=?", (ic,)
                ).fetchone()
                if row is None:
                    raise QuarantineControlError(f"物品 {ic} 不在领用在外清单中")
                if row["teacher"] != teacher:
                    raise QuarantineControlError(
                        f"物品 {ic} 由 {row['teacher']} 领用，不能由 {teacher} 归还"
                    )
                dest = Accounts.AVAILABLE if outcome == "REUSE" else Accounts.DISPOSED
                entries.append((Accounts.ISSUED, ic, -1))
                entries.append((dest, ic, 1))
                traces.append(
                    {
                        "item_code": ic,
                        "action": ACT_GIVE_BACK if outcome == "REUSE" else ACT_GIVE_BACK_DISPOSE,
                        "actor": handled_by or teacher,
                        "location": "可用库房" if outcome == "REUSE" else "",
                        "note": reason or ("归还重新入库" if outcome == "REUSE" else "归还后报废处置"),
                    }
                )
            ledger.post_txn(
                txn_type=TxnType.GIVE_BACK,
                batch_id=next(iter(batch_ids)) if len(batch_ids) == 1 else None,
                summary=f"{teacher} 归还 {len(item_codes)} 件",
                created_by=handled_by or teacher,
                entries=entries,
                traces=traces,
            )
            self.conn.executemany(
                "DELETE FROM active_issues WHERE item_code=?", [(ic,) for ic in item_codes]
            )
        return {"teacher": teacher, "item_codes": item_codes, "outcome": outcome}

    def list_active_issues(self, teacher: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT a.*, i.batch_id FROM active_issues a JOIN items i ON i.item_code=a.item_code"
            + (" WHERE a.teacher = ?" if teacher else "")
            + " ORDER BY a.issued_at"
        )
        params = (teacher,) if teacher else ()
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ================= 查询与追踪 =================

    def list_batches(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT b.*, d.name AS donor_name
            FROM batches b JOIN donors d ON d.donor_id = b.donor_id
            ORDER BY b.batch_id
            """
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["balances"] = Ledger(self.conn).batch_balances(int(r["batch_id"]))
            result.append(item)
        return result

    def get_batch(self, *, batch_no: str) -> dict[str, Any]:
        ledger = Ledger(self.conn)
        batch = ledger.require_batch(batch_no=batch_no)
        batch_id = int(batch["batch_id"])
        donor = ledger.donor_by_id(int(batch["donor_id"]))
        restrictions = self.conn.execute(
            "SELECT seq, restriction, reason, changed_at FROM batch_restrictions WHERE batch_id=? ORDER BY seq",
            (batch_id,),
        ).fetchall()
        inspections = self.conn.execute(
            "SELECT * FROM inspections WHERE batch_id=? ORDER BY seq", (batch_id,)
        ).fetchall()
        inspection_out = []
        for insp in inspections:
            checks = self.conn.execute(
                "SELECT check_name, result, note FROM inspection_checks WHERE inspection_id=? ORDER BY id",
                (insp["id"],),
            ).fetchall()
            inspection_out.append({**dict(insp), "checks": [dict(c) for c in checks]})

        items_out = []
        for it in ledger.batch_items(batch_id):
            ic = it["item_code"]
            balances = ledger.item_balances(ic)
            physical = {a: balances.get(a, 0) for a in Accounts.PHYSICAL if balances.get(a, 0)}
            items_out.append(
                {
                    "item_code": ic,
                    "seq_no": it["seq_no"],
                    "detail": it["detail"],
                    "state": _state_of(balances),
                    "location": ledger.current_location(ic),
                    "balances": physical,
                }
            )

        txns = self.conn.execute(
            """
            SELECT t.*, COUNT(le.id) AS entry_count
            FROM transactions t LEFT JOIN ledger_entries le ON le.txn_id = t.id
            WHERE t.batch_id = ?
            GROUP BY t.id ORDER BY t.id
            """,
            (batch_id,),
        ).fetchall()
        return {
            **dict(batch),
            "donor_name": donor["name"] if donor else "",
            "balances": ledger.batch_balances(batch_id),
            "restrictions": [dict(r) for r in restrictions],
            "inspections": inspection_out,
            "items": items_out,
            "transactions": [dict(t) for t in txns],
        }

    def get_item_trace(self, item_code: str) -> dict[str, Any]:
        ledger = Ledger(self.conn)
        item = ledger.require_item(item_code)
        batch = ledger.batch(batch_id=int(item["batch_id"]))
        rows = self.conn.execute(
            """
            SELECT tr.id, tr.txn_id, tr.action, tr.actor, tr.location, tr.note, tr.created_at,
                   t.txn_type, t.ref_no
            FROM item_trace tr JOIN transactions t ON t.id = tr.txn_id
            WHERE tr.item_code = ?
            ORDER BY tr.id
            """,
            (item_code,),
        ).fetchall()
        balances = ledger.item_balances(item_code)
        return {
            "item_code": item_code,
            "detail": item["detail"],
            "batch_no": batch["batch_no"],
            "voucher_no": batch["voucher_no"],
            "donor_restriction": batch["use_restriction"],
            "state": _state_of(balances),
            "location": ledger.current_location(item_code),
            "balances": {a: balances.get(a, 0) for a in Accounts.PHYSICAL if balances.get(a, 0)},
            "trace": [dict(r) for r in rows],
        }

    def get_transaction(self, txn_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        if row is None:
            raise NotFoundError("事务不存在")
        entries = self.conn.execute(
            "SELECT account, item_code, delta FROM ledger_entries WHERE txn_id=? ORDER BY id",
            (txn_id,),
        ).fetchall()
        return {**dict(row), "entries": [dict(e) for e in entries]}

    # ================= 内部辅助 =================

    def _select_targets(
        self,
        candidates: list[str],
        item_codes: list[str] | None,
        qty: int | None,
        batch_id: int,
        ledger: Ledger,
    ) -> list[str]:
        if item_codes is not None:
            if qty is not None:
                raise ValidationError("item_codes 与 qty 只能二选一")
            if not item_codes:
                raise ValidationError("item_codes 不能为空")
            if len(item_codes) != len(set(item_codes)):
                raise ValidationError("物品编码不能重复")
            allowed = set(candidates)
            bad = [ic for ic in item_codes if ic not in allowed]
            if bad:
                raise QuarantineControlError("物品不在可操作状态：" + "、".join(bad))
            return list(item_codes)
        if qty is None:
            raise ValidationError("必须提供 item_codes 或 qty")
        qty = _as_int(qty, "qty")
        if qty <= 0:
            raise ValidationError("数量必须为正")
        if qty > len(candidates):
            raise InsufficientBalanceError(f"可操作数量不足：需要 {qty}，当前 {len(candidates)}")
        return candidates[:qty]

    def _seq_key(self, ledger: Ledger):
        seq_map = {
            r["item_code"]: r["seq_no"] for r in self.conn.execute("SELECT item_code, seq_no FROM items").fetchall()
        }
        return lambda ic: seq_map.get(ic, 0)

    def _refresh_received_qty(self, ledger: Ledger, batch_id: int) -> None:
        received = int(
            ledger.scalar(
                """
                SELECT COUNT(DISTINCT i.item_code)
                FROM items i JOIN ledger_entries e ON e.item_code = i.item_code
                WHERE i.batch_id = ? AND e.account != '捐赠来源'
                  AND e.account != '待收捐赠'
                """,
                (batch_id,),
            )
        )
        self.conn.execute(
            "UPDATE batches SET received_qty=?, version=version+1 WHERE batch_id=?",
            (received, batch_id),
        )
