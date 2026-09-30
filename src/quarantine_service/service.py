"""捐赠隔离领域服务。

所有状态变更都遵循同一条纪律：在单个 ``BEGIN IMMEDIATE`` 事务里
完成「条件更新库存桶 → 写业务记录 → 写事务分录 → 登记幂等键」，
因此部分接收、退回、限制变更、重复凭证与并发领用始终一致，
且任何时刻都能从捐赠入口追踪到领用、归还或处置结果。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from contextlib import closing
from typing import Any, Callable

from . import db
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)

# 角色（与 domain/contract.json 的 actors 对齐）
ROLE_DONOR = "捐赠方"
ROLE_ADMIN = "学校资产管理员"
ROLE_TEACHER = "使用教师"
ROLES = frozenset({ROLE_DONOR, ROLE_ADMIN, ROLE_TEACHER})

# 用途限制与领用对象
RESTRICTION_UNLIMITED = "无限制"
RESTRICTION_TEACHER_ONLY = "仅限教师使用"
RESTRICTION_NO_STUDENT = "禁止学生使用"
RESTRICTIONS = (RESTRICTION_UNLIMITED, RESTRICTION_TEACHER_ONLY, RESTRICTION_NO_STUDENT)
RESTRICTION_RULES = {
    RESTRICTION_UNLIMITED: frozenset({"教师", "学生"}),
    RESTRICTION_TEACHER_ONLY: frozenset({"教师"}),
    RESTRICTION_NO_STUDENT: frozenset({"教师"}),
}
USED_BY_ALL = frozenset({"教师", "学生"})

# 验收结论
ACCEPT_PASS = "合格"
ACCEPT_FAIL = "不合格"
ACCEPT_RESULTS = frozenset({ACCEPT_PASS, ACCEPT_FAIL})

# 归还成色
CONDITION_REUSABLE = "可再用"
CONDITION_DAMAGED = "不可再用"
RETURN_CONDITIONS = frozenset({CONDITION_REUSABLE, CONDITION_DAMAGED})

# 库存桶：每件物品的数量在五个桶之间迁移，总和恒等于已接收数量
BUCKET_QUARANTINED = "隔离"
BUCKET_USABLE = "可用"
BUCKET_CHECKED_OUT = "领用"
BUCKET_RETURNED = "退回捐赠方"
BUCKET_DISPOSED = "处置"
BUCKETS = (BUCKET_QUARANTINED, BUCKET_USABLE, BUCKET_CHECKED_OUT, BUCKET_RETURNED, BUCKET_DISPOSED)
_BUCKET_COLUMNS = {
    BUCKET_QUARANTINED: "qty_quarantined",
    BUCKET_USABLE: "qty_usable",
    BUCKET_CHECKED_OUT: "qty_checked_out",
    BUCKET_RETURNED: "qty_returned_donor",
    BUCKET_DISPOSED: "qty_disposed",
}

# 批次状态（与契约 states 对齐，只能前进）
BATCH_STATUSES = ("登记", "隔离", "验收", "放行", "领用")

# 分录动作
ACT_REGISTER = "登记批次"
ACT_RECEIPT = "部分接收"
ACT_ACCEPTANCE = "验收登记"
ACT_RELEASE = "放行"
ACT_RETURN_DONOR = "退回捐赠方"
ACT_DISPOSE = "处置"
ACT_RESTRICTION = "限制变更"
ACT_CHECKOUT = "领用"
ACT_RETURN = "归还"
ACT_RETURN_DAMAGED = "归还报废"


class QuarantineService:
    """捐赠隔离服务入口：每个公开方法对应一类事务。"""

    def __init__(self, db_path: str, *, clock: Callable[[], str] | None = None) -> None:
        self._db_path = str(db_path)
        db.init_db(self._db_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    # ------------------------------------------------------------------
    # 基础助手
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return self._clock()

    @staticmethod
    def _require_actor(actor: Any, role: Any) -> tuple[str, str]:
        if not isinstance(actor, str) or not actor.strip():
            raise UnauthorizedError("缺少操作人")
        if not isinstance(role, str) or role not in ROLES:
            raise UnauthorizedError("缺少或未知的角色")
        return actor.strip(), role

    @staticmethod
    def _require_role(role: str, allowed: frozenset[str] | set[str]) -> None:
        if role not in allowed:
            raise ForbiddenError("当前角色无权执行该操作")

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field}不能为空")
        return value.strip()

    @staticmethod
    def _optional_text(value: Any, field: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValidationError(f"{field}必须是字符串")
        return value.strip()

    @staticmethod
    def _require_qty(value: Any, field: str = "数量") -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationError(f"{field}必须是正整数")
        return value

    # ------------------------------------------------------------------
    # 事务与幂等
    # ------------------------------------------------------------------
    def _execute(self, request_key: Any, action: str, work: Callable[[sqlite3.Connection], dict]) -> dict:
        """在单事务内执行 work，并按请求键去重（重复提交返回首个响应）。"""
        if request_key is not None:
            request_key = self._require_text(request_key, "请求键")
        try:
            with db.transaction(self._db_path) as conn:
                if request_key:
                    replay = self._lookup_request(conn, request_key)
                    if replay is not None:
                        replay["replayed"] = True
                        return replay
                result = work(conn)
                result["replayed"] = False
                self._record_request(conn, request_key, action, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise ConflictError("相同的凭证或请求已存在", code="DUPLICATE_REQUEST") from exc

    def _lookup_request(self, conn: sqlite3.Connection, key: str) -> dict | None:
        row = conn.execute("SELECT response FROM request_keys WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row["response"])

    def _record_request(self, conn: sqlite3.Connection, key: str | None, action: str, response: dict) -> None:
        if not key:
            return
        conn.execute(
            "INSERT INTO request_keys (key, action, response, created_ts) VALUES (?,?,?,?)",
            (key, action, json.dumps(response, ensure_ascii=False), self._now()),
        )

    def _ledger(
        self,
        conn: sqlite3.Connection,
        *,
        actor: str,
        role: str,
        action: str,
        batch_id: int | None = None,
        item_id: int | None = None,
        checkout_id: int | None = None,
        quantity: int | None = None,
        from_bucket: str | None = None,
        to_bucket: str | None = None,
        request_key: str | None = None,
        detail: dict | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO ledger_entries (ts, actor, role, action, batch_id, item_id, checkout_id,"
            " quantity, from_bucket, to_bucket, request_key, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._now(),
                actor,
                role,
                action,
                batch_id,
                item_id,
                checkout_id,
                quantity,
                from_bucket,
                to_bucket,
                request_key,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )

    # ------------------------------------------------------------------
    # 行级助手
    # ------------------------------------------------------------------
    @staticmethod
    def _get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("物品不存在")
        return row

    def _advance_batch(self, conn: sqlite3.Connection, batch_id: int, target: str) -> None:
        row = conn.execute("SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if BATCH_STATUSES.index(target) > BATCH_STATUSES.index(row["status"]):
            conn.execute("UPDATE batches SET status = ? WHERE id = ?", (target, batch_id))

    def _move(
        self,
        conn: sqlite3.Connection,
        item_id: int,
        deltas: dict[str, int],
        *,
        guard_bucket: str,
        guard_min: int,
        error: tuple[str, str],
    ) -> None:
        """条件更新库存桶；来源桶不足时整体失败，绝不出现负库存。"""
        assignments = ", ".join(f"{_BUCKET_COLUMNS[b]} = {_BUCKET_COLUMNS[b]} + ?" for b in deltas)
        sql = f"UPDATE items SET {assignments} WHERE id = ? AND {_BUCKET_COLUMNS[guard_bucket]} >= ?"
        params = [deltas[b] for b in deltas] + [item_id, guard_min]
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            code, message = error
            raise ConflictError(message, code=code)

    # ------------------------------------------------------------------
    # 读模型
    # ------------------------------------------------------------------
    def _item_row_payload(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        buckets = {name: row[col] for name, col in _BUCKET_COLUMNS.items()}
        acceptance = conn.execute(
            "SELECT check_item, result, note, inspector, created_ts"
            " FROM acceptance_records WHERE item_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "batch_id": row["batch_id"],
            "name": row["name"],
            "category": row["category"],
            "usage_restriction": row["usage_restriction"],
            "storage_location": row["storage_location"],
            "qty_expected": row["qty_expected"],
            "qty_received": sum(buckets.values()),
            "buckets": buckets,
            "acceptance_records": [dict(r) for r in acceptance],
        }

    def _item_payload(self, conn: sqlite3.Connection, item_id: int) -> dict:
        return self._item_row_payload(conn, self._get_item(conn, item_id))

    def _batch_payload(self, conn: sqlite3.Connection, batch_id: int) -> dict:
        row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        items = conn.execute("SELECT * FROM items WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
        return {
            "id": row["id"],
            "voucher_no": row["voucher_no"],
            "donor": row["donor"],
            "source_note": row["source_note"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_ts": row["created_ts"],
            "items": [self._item_row_payload(conn, item) for item in items],
        }

    def _checkout_payload(self, conn: sqlite3.Connection, checkout_id: int) -> dict:
        row = conn.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
        if row is None:
            raise NotFoundError("领用记录不存在")
        return {
            "id": row["id"],
            "item_id": row["item_id"],
            "borrower": row["borrower"],
            "used_by": row["used_by"],
            "purpose": row["purpose"],
            "quantity": row["quantity"],
            "qty_returned": row["qty_returned"],
            "qty_damaged": row["qty_damaged"],
            "qty_outstanding": row["quantity"] - row["qty_returned"] - row["qty_damaged"],
            "status": row["status"],
            "created_ts": row["created_ts"],
        }

    @staticmethod
    def _ledger_row_payload(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "ts": row["ts"],
            "actor": row["actor"],
            "role": row["role"],
            "action": row["action"],
            "batch_id": row["batch_id"],
            "item_id": row["item_id"],
            "checkout_id": row["checkout_id"],
            "quantity": row["quantity"],
            "from_bucket": row["from_bucket"],
            "to_bucket": row["to_bucket"],
            "request_key": row["request_key"],
            "detail": json.loads(row["detail"]),
        }

    # ------------------------------------------------------------------
    # 批次登记（重复凭证幂等）
    # ------------------------------------------------------------------
    def create_batch(
        self,
        *,
        voucher_no: Any,
        donor: Any,
        items: Any,
        actor: Any,
        role: Any,
        source_note: Any = "",
        request_key: Any = None,
    ) -> dict:
        actor, role = self._require_actor(actor, role)
        self._require_role(role, {ROLE_ADMIN, ROLE_DONOR})
        voucher_no = self._require_text(voucher_no, "凭证号")
        donor = self._require_text(donor, "捐赠方")
        source_note = self._optional_text(source_note, "来源说明")
        if not isinstance(items, list) or not items:
            raise ValidationError("物品清单不能为空")
        normalized = [self._normalize_item(raw) for raw in items]

        def work(conn: sqlite3.Connection) -> dict:
            existing = conn.execute("SELECT id FROM batches WHERE voucher_no = ?", (voucher_no,)).fetchone()
            if existing is not None:
                # 重复凭证：返回已登记的批次，不产生任何新分录
                result = self._batch_payload(conn, existing["id"])
                result["duplicate"] = True
                return result
            ts = self._now()
            cur = conn.execute(
                "INSERT INTO batches (voucher_no, donor, source_note, status, created_by, created_ts)"
                " VALUES (?,?,?,?,?,?)",
                (voucher_no, donor, source_note, "登记", actor, ts),
            )
            batch_id = cur.lastrowid
            for item in normalized:
                conn.execute(
                    "INSERT INTO items (batch_id, name, category, usage_restriction, storage_location,"
                    " qty_expected, created_ts) VALUES (?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        item["name"],
                        item["category"],
                        item["usage_restriction"],
                        item["storage_location"],
                        item["qty_expected"],
                        ts,
                    ),
                )
            self._ledger(
                conn,
                actor=actor,
                role=role,
                action=ACT_REGISTER,
                batch_id=batch_id,
                request_key=request_key,
                detail={"voucher_no": voucher_no, "donor": donor, "item_count": len(normalized)},
            )
            result = self._batch_payload(conn, batch_id)
            result["duplicate"] = False
            return result

        return self._execute(request_key, ACT_REGISTER, work)

    def _normalize_item(self, raw: Any) -> dict:
        if not isinstance(raw, dict):
            raise ValidationError("物品条目格式不正确")
        restriction = raw.get("usage_restriction", RESTRICTION_UNLIMITED)
        if restriction not in RESTRICTION_RULES:
            raise ValidationError("未知的用途限制")
        return {
            "name": self._require_text(raw.get("name"), "物品名称"),
            "category": self._optional_text(raw.get("category"), "物品类别"),
            "usage_restriction": restriction,
            "storage_location": self._require_text(raw.get("storage_location"), "保管位置"),
            "qty_expected": self._require_qty(raw.get("qty_expected"), "登记数量"),
        }

    # ------------------------------------------------------------------
    # 部分接收
    # ------------------------------------------------------------------
    def add_receipt(
        self,
        item_id: int,
        quantity: Any,
        *,
        actor: Any,
        role: Any,
        note: Any = "",
        request_key: Any = None,
    ) -> dict:
        actor, role = self._require_actor(actor, role)
        self._require_role(role, {ROLE_ADMIN})
        quantity = self._require_qty(quantity, "接收数量")
        note = self._optional_text(note, "备注")

        def work(conn: sqlite3.Connection) -> dict:
            item = self._get_item(conn, item_id)
            cur = conn.execute(
                "UPDATE items SET qty_quarantined = qty_quarantined + ?"
                " WHERE id = ?"
                " AND qty_quarantined + qty_usable + qty_checked_out + qty_returned_donor + qty_disposed + ?"
                "    <= qty_expected",
                (quantity, item_id, quantity),
            )
            if cur.rowcount != 1:
                raise ConflictError("接收数量超出登记数量", code="EXCEEDS_EXPECTED")
            self._advance_batch(conn, item["batch_id"], "隔离")
            self._ledger(
                conn,
                actor=actor,
                role=role,
                action=ACT_RECEIPT,
                batch_id=item["batch_id"],
                item_id=item_id,
                quantity=quantity,
                to_bucket=BUCKET_QUARANTINED,
                request_key=request_key,
                detail={"note": note},
            )
            return {"item": self._item_payload(conn, item_id)}

        return self._execute(request_key, ACT_RECEIPT, work)

    # ------------------------------------------------------------------
    # 验收项目
    # ------------------------------------------------------------------
    def add_acceptance(
        self,
        item_id: int,
        check_item: Any,
        result: Any,
        *,
        actor: Any,
        role: Any,
        note: Any = "",
        request_key: Any = None,
    ) -> dict:
        actor, role = self._require_actor(actor, role)
        self._require_role(role, {ROLE_ADMIN})
        check_item = self._require_text(check_item, "验收项目")
        if result not in ACCEPT_RESULTS:
            raise ValidationError("验收结论必须是合格或不合格")
        note = self._optional_text(note, "备注")

        def work(conn: sqlite3.Connection) -> dict:
            item = self._get_item(conn, item_id)
            conn.execute(
                "INSERT INTO acceptance_records (item_id, check_item, result, note, inspector, created_ts)"
                " VALUES (?,?,?,?,?,?)",
                (item_id, check_item, result, note, actor, self._now()),
            )
            self._advance_batch(conn, item["batch_id"], "验收")
            self._ledger(
                conn,
                actor=actor,
                role=role,
                action=ACT_ACCEPTANCE,
                batch_id=item["batch_id"],
                item_id=item_id,
                request_key=request_key,
                detail={"check_item": check_item, "result": result, "note": note},
            )
            return {"item": self._item_payload(conn, item_id)}

        return self._execute(request_key, ACT_ACCEPTANCE, work)

    # ------------------------------------------------------------------
    # 放行决定：只有批准数量才能从隔离转入可用
    # ------------------------------------------------------------------
    def release(
        self,
        item_id: int,
        quantity: Any,
        *,
        actor: Any,
        role: Any,
        note: Any = "",
        request_key: Any = None,
    ) -> dict:
        actor, role = self._require_actor(actor, role)
        self._require_role(role, {ROLE_ADMIN})
        quantity = self._require_qty(quantity, "放行数量")
        note = self._optional_text(note, "备注")

        def work(conn: sqlite3.Connection) -> dict:
            item = self._get_item(conn, item_id)
            failed = conn.execute(
                "SELECT COUNT(*) AS c FROM acceptance_records WHERE item_id = ? AND result = ?",
                (item_id, ACCEPT_FAIL),
            ).fetchone()["c"]
            if failed:
                raise ConflictError("存在不合格验收记录，禁止放行", code="ACCEPTANCE_FAILED")
            passed = conn.execute(
                "SELECT COUNT(*) AS c FROM acceptance_records WHERE item_id = ? AND result = ?",
                (item_id, ACCEPT_PASS),
            ).fetchone()["c"]
            if not passed:
                raise ConflictError("验收未完成，禁止放行", code="ACCEPTANCE_INCOMPLETE")
            self._move(
                conn,
                item_id,
                {BUCKET_QUARANTINED: -quantity, BUCKET_USABLE: quantity},
                guard_bucket=BUCKET_QUARANTINED,
                guard_min=quantity,
                error=("INSUFFICIENT_QUARANTINED", "隔离库存不足"),
            )
            self._advance_batch(conn, item["batch_id"], "放行")
            self._ledger(
                conn,
                actor=actor,
                role=role,
                action=ACT_RELEASE,
                batch_id=item["batch_id"],
                item_id=item_id,
                quantity=quantity,
                from_bucket=BUCKET_QUARANTINED,
                to_bucket=BUCKET_USABLE,
                request_key=request_key,
                detail={"note": note},
            )
            return {"item": self._item_payload(conn, item_id)}

        return self._execute(request_key, ACT_RELEASE, work)

    # ------------------------------------------------------------------
    # 退回捐赠方 / 处置
    # ------------------------------------------------------------------
    def return_to_donor(
        self,
        item_id: int,
        quantity: Any,
        from_bucket: Any,
        reason: Any,
        *,
        actor: Any,
        role: Any,
        request_key: Any = None,
    ) -> dict:
        actor, role = self._require_actor(actor, role)
        self._require_role(role, {ROLE_ADMIN})
        quantity = self._require_qty(quantity, "退回数量")
        if from_bucket not in (BUCKET_QUARANTINED, BUCKET_USABLE):
            raise ValidationError("退回来源必须是隔离或可用库存")
        reason = self._require_text(reason, "退回原因")

        def work(conn: sqlite3.Connection) -> dict:
            item = self._get_item(conn, item_id)
            self._move(
                conn,
                item_id,
                {from_bucket: -quantity, BUCKET_RETURNED: quantity},
                guard_bucket=from_bucket,
                guard_min=quantity,
                error=("INSUFFICIENT_QUANTITY", "库存数量不足"),
            )
            self._ledger(
                conn,
                actor=actor,
                role=role,
                action=ACT_RETURN_DONOR,
                batch_id=item["batch_id"],
                item_id=item_id,
                quantity=quantity,
                from_bucket=from_bucket,
                to_bucket=BUCKET_RETURNED,
                request_key=request_key,
                detail={"reason": reason},
            )
            return {"item": self._item_payload(conn, item_id)}

        return self._execute(request_key, ACT_RETURN_DONOR, work)

    def dispose(
        self,
        item_id: int,
        quantity: Any,
        from_bucket: Any,
        reason: Any,
        *,
        actor: Any,
        role: Any,
        request_key: Any = None,
    ) -> dict:
        actor, role = self._require_actor(actor, role)
        self._require_role(role, {ROLE_ADMIN})
        quantity = self._require_qty(quantity, "处置数量")
        if from_bucket not in (BUCKET_QUARANTINED, BUCKET_USABLE):
            raise ValidationError("处置来源必须是隔离或可用库存")
        reason = self._require_text(reason, "处置原因")

        def work(conn: sqlite3.Connection) -> dict:
            item = self._get_item(conn, item_id)
            self._move(
                conn,
                item_id,
                {from_bucket: -quantity, BUCKET_DISPOSED: quantity},
                guard_bucket=from_bucket,
                guard_min=quantity,
                error=("INSUFFICIENT_QUANTITY", "库存数量不足"),
            )
            self._ledger(
                conn,
                actor=actor,
                role=role,
                action=ACT_DISPOSE,
                batch_id=item["batch_id"],
                item_id=item_id,
                quantity=quantity,
                from_bucket=from_bucket,
                to_bucket=BUCKET_DISPOSED,
                request_key=request_key,
                detail={"reason": reason},
            )
            return {"item": self._item_payload(conn, item_id)}

        return self._execute(request_key, ACT_DISPOSE, work)

    # ------------------------------------------------------------------
    # 限制变更
    # ------------------------------------------------------------------
    def change_restriction(
        self,
        item_id: int,
        new_restriction: Any,
        reason: Any,
        *,
        actor: Any,
        role: Any,
        request_key: Any = None,
    ) -> dict:
        actor, role = self._require_actor(actor, role)
        self._require_role(role, {ROLE_ADMIN})
        if new_restriction not in RESTRICTION_RULES:
            raise ValidationError("未知的用途限制")
        reason = self._require_text(reason, "变更原因")

        def work(conn: sqlite3.Connection) -> dict:
            item = self._get_item(conn, item_id)
            old = item["usage_restriction"]
            if old == new_restriction:
                raise ValidationError("新限制与当前限制相同")
            conn.execute("UPDATE items SET usage_restriction = ? WHERE id = ?", (new_restriction, item_id))
            self._ledger(
                conn,
                actor=actor,
                role=role,
                action=ACT_RESTRICTION,
                batch_id=item["batch_id"],
                item_id=item_id,
                request_key=request_key,
                detail={"old": old, "new": new_restriction, "reason": reason},
            )
            return {"item": self._item_payload(conn, item_id)}

        return self._execute(request_key, ACT_RESTRICTION, work)

    # ------------------------------------------------------------------
    # 领用（并发安全：条件更新 + 写事务串行化）
    # ------------------------------------------------------------------
    def checkout(
        self,
        item_id: int,
        quantity: Any,
        *,
        borrower: Any,
        used_by: Any,
        actor: Any,
        role: Any,
        purpose: Any = "",
        request_key: Any = None,
    ) -> dict:
        actor, role = self._require_actor(actor, role)
        self._require_role(role, {ROLE_TEACHER, ROLE_ADMIN})
        quantity = self._require_qty(quantity, "领用数量")
        borrower = self._require_text(borrower, "领用人")
        if used_by not in USED_BY_ALL:
            raise ValidationError("使用对象必须是教师或学生")
        purpose = self._optional_text(purpose, "用途")

        def work(conn: sqlite3.Connection) -> dict:
            item = self._get_item(conn, item_id)
            restriction = item["usage_restriction"]
            if used_by not in RESTRICTION_RULES[restriction]:
                raise ConflictError(f"用途限制「{restriction}」不允许{used_by}领用", code="RESTRICTION_VIOLATION")
            self._move(
                conn,
                item_id,
                {BUCKET_USABLE: -quantity, BUCKET_CHECKED_OUT: quantity},
                guard_bucket=BUCKET_USABLE,
                guard_min=quantity,
                error=("INSUFFICIENT_USABLE", "可用库存不足"),
            )
            cur = conn.execute(
                "INSERT INTO checkouts (item_id, borrower, used_by, purpose, quantity, status, request_key, created_ts)"
                " VALUES (?,?,?,?,?,'在借',?,?)",
                (item_id, borrower, used_by, purpose, quantity, request_key, self._now()),
            )
            checkout_id = cur.lastrowid
            self._advance_batch(conn, item["batch_id"], "领用")
            self._ledger(
                conn,
                actor=actor,
                role=role,
                action=ACT_CHECKOUT,
                batch_id=item["batch_id"],
                item_id=item_id,
                checkout_id=checkout_id,
                quantity=quantity,
                from_bucket=BUCKET_USABLE,
                to_bucket=BUCKET_CHECKED_OUT,
                request_key=request_key,
                detail={"borrower": borrower, "used_by": used_by, "purpose": purpose},
            )
            return {
                "checkout": self._checkout_payload(conn, checkout_id),
                "item": self._item_payload(conn, item_id),
            }

        return self._execute(request_key, ACT_CHECKOUT, work)

    # ------------------------------------------------------------------
    # 归还（可部分归还；不可再用的部分直接计入处置）
    # ------------------------------------------------------------------
    def return_checkout(
        self,
        checkout_id: int,
        quantity: Any,
        condition: Any,
        *,
        actor: Any,
        role: Any,
        note: Any = "",
        request_key: Any = None,
    ) -> dict:
        actor, role = self._require_actor(actor, role)
        self._require_role(role, {ROLE_TEACHER, ROLE_ADMIN})
        quantity = self._require_qty(quantity, "归还数量")
        if condition not in RETURN_CONDITIONS:
            raise ValidationError("归还成色必须是可再用或不可再用")
        note = self._optional_text(note, "备注")

        def work(conn: sqlite3.Connection) -> dict:
            row = conn.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
            if row is None:
                raise NotFoundError("领用记录不存在")
            if condition == CONDITION_REUSABLE:
                column, to_bucket, action = "qty_returned", BUCKET_USABLE, ACT_RETURN
            else:
                column, to_bucket, action = "qty_damaged", BUCKET_DISPOSED, ACT_RETURN_DAMAGED
            cur = conn.execute(
                f"UPDATE checkouts SET {column} = {column} + ?"
                " WHERE id = ? AND quantity - qty_returned - qty_damaged >= ?",
                (quantity, checkout_id, quantity),
            )
            if cur.rowcount != 1:
                remaining = row["quantity"] - row["qty_returned"] - row["qty_damaged"]
                if remaining <= 0:
                    raise ConflictError("该领用已全部归还", code="CHECKOUT_CLOSED")
                raise ConflictError("归还数量超出未还数量", code="RETURN_EXCEEDS")
            self._move(
                conn,
                row["item_id"],
                {BUCKET_CHECKED_OUT: -quantity, to_bucket: quantity},
                guard_bucket=BUCKET_CHECKED_OUT,
                guard_min=quantity,
                error=("INSUFFICIENT_QUANTITY", "库存数量不足"),
            )
            conn.execute(
                "UPDATE checkouts SET status = '已归还' WHERE id = ? AND quantity = qty_returned + qty_damaged",
                (checkout_id,),
            )
            item = self._get_item(conn, row["item_id"])
            self._ledger(
                conn,
                actor=actor,
                role=role,
                action=action,
                batch_id=item["batch_id"],
                item_id=row["item_id"],
                checkout_id=checkout_id,
                quantity=quantity,
                from_bucket=BUCKET_CHECKED_OUT,
                to_bucket=to_bucket,
                request_key=request_key,
                detail={"condition": condition, "note": note},
            )
            return {
                "checkout": self._checkout_payload(conn, checkout_id),
                "item": self._item_payload(conn, row["item_id"]),
            }

        return self._execute(request_key, ACT_RETURN, work)

    # ------------------------------------------------------------------
    # 查询与追踪
    # ------------------------------------------------------------------
    def get_batch(self, batch_id: int) -> dict:
        with closing(db.connect(self._db_path)) as conn:
            return self._batch_payload(conn, batch_id)

    def list_batches(self) -> dict:
        with closing(db.connect(self._db_path)) as conn:
            rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
            return {"batches": [self._batch_summary(conn, row) for row in rows]}

    def _batch_summary(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        agg = conn.execute(
            "SELECT COUNT(*) AS item_count,"
            " COALESCE(SUM(qty_expected), 0) AS expected,"
            " COALESCE(SUM(qty_quarantined), 0) AS q,"
            " COALESCE(SUM(qty_usable), 0) AS u,"
            " COALESCE(SUM(qty_checked_out), 0) AS c,"
            " COALESCE(SUM(qty_returned_donor), 0) AS r,"
            " COALESCE(SUM(qty_disposed), 0) AS d"
            " FROM items WHERE batch_id = ?",
            (row["id"],),
        ).fetchone()
        buckets = {
            BUCKET_QUARANTINED: agg["q"],
            BUCKET_USABLE: agg["u"],
            BUCKET_CHECKED_OUT: agg["c"],
            BUCKET_RETURNED: agg["r"],
            BUCKET_DISPOSED: agg["d"],
        }
        return {
            "id": row["id"],
            "voucher_no": row["voucher_no"],
            "donor": row["donor"],
            "status": row["status"],
            "created_ts": row["created_ts"],
            "item_count": agg["item_count"],
            "totals": {
                "qty_expected": agg["expected"],
                "qty_received": sum(buckets.values()),
                "buckets": buckets,
            },
        }

    def get_item(self, item_id: int) -> dict:
        with closing(db.connect(self._db_path)) as conn:
            return self._item_payload(conn, item_id)

    def list_inventory(self) -> dict:
        """当前可用库存（含来源批次，便于追溯）。"""
        with closing(db.connect(self._db_path)) as conn:
            rows = conn.execute(
                "SELECT i.*, b.voucher_no AS voucher_no, b.donor AS donor"
                " FROM items i JOIN batches b ON b.id = i.batch_id"
                " WHERE i.qty_usable > 0 ORDER BY i.id"
            ).fetchall()
            items = []
            for row in rows:
                payload = self._item_row_payload(conn, row)
                payload["voucher_no"] = row["voucher_no"]
                payload["donor"] = row["donor"]
                items.append(payload)
            return {"items": items}

    def item_trace(self, item_id: int) -> dict:
        """单件物品的完整去向：批次级分录 + 物品级分录，按发生顺序排列。"""
        with closing(db.connect(self._db_path)) as conn:
            item = self._item_payload(conn, item_id)
            rows = conn.execute(
                "SELECT * FROM ledger_entries"
                " WHERE item_id = ? OR (batch_id = ? AND item_id IS NULL)"
                " ORDER BY id",
                (item_id, item["batch_id"]),
            ).fetchall()
            return {"item": item, "entries": [self._ledger_row_payload(r) for r in rows]}

    def batch_trace(self, batch_id: int) -> dict:
        """从捐赠入口到领用、归还或处置的完整分录链。"""
        with closing(db.connect(self._db_path)) as conn:
            batch = self._batch_payload(conn, batch_id)
            rows = conn.execute(
                "SELECT * FROM ledger_entries WHERE batch_id = ? ORDER BY id", (batch_id,)
            ).fetchall()
            return {"batch": batch, "entries": [self._ledger_row_payload(r) for r in rows]}

    def list_ledger(self, *, batch_id: int | None = None, item_id: int | None = None, limit: int = 200) -> dict:
        clauses, params = [], []
        if batch_id is not None:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if item_id is not None:
            clauses.append("item_id = ?")
            params.append(item_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 1000))
        with closing(db.connect(self._db_path)) as conn:
            rows = conn.execute(
                f"SELECT * FROM ledger_entries{where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
            return {"entries": [self._ledger_row_payload(r) for r in rows]}
