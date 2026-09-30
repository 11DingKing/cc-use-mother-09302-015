"""捐赠隔离领域服务的行为测试。"""
from __future__ import annotations

import itertools
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quarantine_service.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from quarantine_service.service import QuarantineService

ADMIN = {"actor": "张管理员", "role": "学校资产管理员"}
TEACHER = {"actor": "李老师", "role": "使用教师"}
DONOR = {"actor": "王代表", "role": "捐赠方"}


def _clock():
    base = datetime(2026, 9, 30, 8, 0, 0, tzinfo=timezone.utc)
    ticks = itertools.count()
    return lambda: (base + timedelta(seconds=next(ticks))).isoformat()


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = QuarantineService(str(Path(self.tmp.name) / "test.db"), clock=_clock())
        self.vouchers = itertools.count(1)

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------
    @staticmethod
    def item_spec(**overrides) -> dict:
        spec = {
            "name": "民族服饰",
            "category": "服饰",
            "qty_expected": 10,
            "usage_restriction": "无限制",
            "storage_location": "隔离库A-01",
        }
        spec.update(overrides)
        return spec

    def create_batch(self, **overrides) -> dict:
        params = {
            "voucher_no": f"PZ-{next(self.vouchers):04d}",
            "donor": "某社会机构",
            "items": [self.item_spec()],
            **ADMIN,
        }
        params.update(overrides)
        return self.service.create_batch(**params)

    def receive(self, item_id, quantity, **kw):
        return self.service.add_receipt(item_id, quantity, **{**ADMIN, **kw})

    def accept(self, item_id, result="合格", check_item="材质安全检测"):
        return self.service.add_acceptance(item_id, check_item, result, **ADMIN)

    def release(self, item_id, quantity, **kw):
        return self.service.release(item_id, quantity, **{**ADMIN, **kw})

    def checkout(self, item_id, quantity, used_by="教师", **kw):
        return self.service.checkout(
            item_id, quantity, borrower="李老师", used_by=used_by, **{**TEACHER, **kw}
        )

    def released_item(self, expected=10, received=None, released=None, restriction="无限制"):
        """构造一件已接收、已验收、已部分或全部放行的物品。"""
        batch = self.create_batch(items=[self.item_spec(qty_expected=expected, usage_restriction=restriction)])
        item_id = batch["items"][0]["id"]
        received = expected if received is None else received
        self.receive(item_id, received)
        self.accept(item_id)
        self.release(item_id, received if released is None else released)
        return batch["id"], item_id

    # ------------------------------------------------------------------
    # 部分接收
    # ------------------------------------------------------------------
    def test_partial_receipt_accumulates_and_caps_at_expected(self) -> None:
        batch = self.create_batch()
        item_id = batch["items"][0]["id"]
        self.assertEqual(batch["status"], "登记")
        self.assertEqual(batch["items"][0]["storage_location"], "隔离库A-01")

        first = self.receive(item_id, 4)
        self.assertEqual(first["item"]["buckets"]["隔离"], 4)
        self.assertEqual(first["item"]["qty_received"], 4)
        self.assertEqual(self.service.get_batch(batch["id"])["status"], "隔离")

        second = self.receive(item_id, 6)
        self.assertEqual(second["item"]["qty_received"], 10)
        with self.assertRaises(ConflictError) as ctx:
            self.receive(item_id, 1)
        self.assertEqual(ctx.exception.code, "EXCEEDS_EXPECTED")

    # ------------------------------------------------------------------
    # 验收与放行
    # ------------------------------------------------------------------
    def test_release_requires_completed_acceptance(self) -> None:
        batch = self.create_batch()
        item_id = batch["items"][0]["id"]
        self.receive(item_id, 5)
        with self.assertRaises(ConflictError) as ctx:
            self.release(item_id, 5)
        self.assertEqual(ctx.exception.code, "ACCEPTANCE_INCOMPLETE")
        # 未放行的物品不能进入可用库存
        self.assertEqual(self.service.get_item(item_id)["buckets"]["可用"], 0)

    def test_failed_acceptance_blocks_release_and_returns_to_donor(self) -> None:
        batch = self.create_batch()
        item_id = batch["items"][0]["id"]
        self.receive(item_id, 10)
        self.accept(item_id, result="不合格")
        with self.assertRaises(ConflictError) as ctx:
            self.release(item_id, 3)
        self.assertEqual(ctx.exception.code, "ACCEPTANCE_FAILED")

        result = self.service.return_to_donor(item_id, 10, "隔离", "材质不合格", **ADMIN)
        self.assertEqual(result["item"]["buckets"]["退回捐赠方"], 10)
        self.assertEqual(result["item"]["buckets"]["隔离"], 0)

    def test_partial_release_moves_only_approved_quantity(self) -> None:
        batch = self.create_batch()
        item_id = batch["items"][0]["id"]
        self.receive(item_id, 10)
        self.accept(item_id)

        first = self.release(item_id, 4)
        self.assertEqual(first["item"]["buckets"]["隔离"], 6)
        self.assertEqual(first["item"]["buckets"]["可用"], 4)
        self.assertEqual(self.service.get_batch(batch["id"])["status"], "放行")

        with self.assertRaises(ConflictError) as ctx:
            self.release(item_id, 7)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_QUARANTINED")

        second = self.release(item_id, 6)
        self.assertEqual(second["item"]["buckets"]["可用"], 10)
        self.assertEqual(second["item"]["buckets"]["隔离"], 0)

    # ------------------------------------------------------------------
    # 领用与归还
    # ------------------------------------------------------------------
    def test_checkout_limited_to_usable_stock(self) -> None:
        batch_id, item_id = self.released_item(expected=10, released=3)
        with self.assertRaises(ConflictError) as ctx:
            self.checkout(item_id, 4)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_USABLE")

        result = self.checkout(item_id, 2)
        self.assertEqual(result["item"]["buckets"]["可用"], 1)
        self.assertEqual(result["item"]["buckets"]["领用"], 2)
        self.assertEqual(result["checkout"]["status"], "在借")
        self.assertEqual(self.service.get_batch(batch_id)["status"], "领用")

    def test_checkout_return_lifecycle(self) -> None:
        _, item_id = self.released_item(expected=10, released=6)
        checkout_id = self.checkout(item_id, 5)["checkout"]["id"]

        first = self.service.return_checkout(checkout_id, 2, "可再用", **TEACHER)
        self.assertEqual(first["item"]["buckets"]["可用"], 3)
        self.assertEqual(first["item"]["buckets"]["领用"], 3)
        self.assertEqual(first["checkout"]["qty_outstanding"], 3)
        self.assertEqual(first["checkout"]["status"], "在借")

        second = self.service.return_checkout(checkout_id, 1, "不可再用", **TEACHER)
        self.assertEqual(second["item"]["buckets"]["处置"], 1)
        self.assertEqual(second["checkout"]["qty_outstanding"], 2)

        with self.assertRaises(ConflictError) as ctx:
            self.service.return_checkout(checkout_id, 3, "可再用", **TEACHER)
        self.assertEqual(ctx.exception.code, "RETURN_EXCEEDS")

        final = self.service.return_checkout(checkout_id, 2, "可再用", **TEACHER)
        self.assertEqual(final["checkout"]["status"], "已归还")
        with self.assertRaises(ConflictError) as ctx:
            self.service.return_checkout(checkout_id, 1, "可再用", **TEACHER)
        self.assertEqual(ctx.exception.code, "CHECKOUT_CLOSED")

    # ------------------------------------------------------------------
    # 捐赠限制与限制变更
    # ------------------------------------------------------------------
    def test_restriction_change_controls_student_use(self) -> None:
        _, item_id = self.released_item(restriction="禁止学生使用")
        self.checkout(item_id, 1, used_by="教师")

        with self.assertRaises(ConflictError) as ctx:
            self.checkout(item_id, 1, used_by="学生")
        self.assertEqual(ctx.exception.code, "RESTRICTION_VIOLATION")

        with self.assertRaises(ValidationError):
            self.service.change_restriction(item_id, "禁止学生使用", "无变化", **ADMIN)

        changed = self.service.change_restriction(item_id, "无限制", "复核后确认对学生安全", **ADMIN)
        self.assertEqual(changed["item"]["usage_restriction"], "无限制")
        self.checkout(item_id, 1, used_by="学生")

        trace = self.service.item_trace(item_id)
        entries = [e for e in trace["entries"] if e["action"] == "限制变更"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["detail"]["old"], "禁止学生使用")
        self.assertEqual(entries[0]["detail"]["new"], "无限制")
        self.assertEqual(entries[0]["detail"]["reason"], "复核后确认对学生安全")

    # ------------------------------------------------------------------
    # 重复凭证与幂等键
    # ------------------------------------------------------------------
    def test_duplicate_voucher_returns_existing_batch(self) -> None:
        first = self.create_batch()
        again = self.service.create_batch(
            voucher_no=first["voucher_no"],
            donor="另一家机构",
            items=[self.item_spec(name="完全不同的物品")],
            **ADMIN,
        )
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(again["items"]), 1)
        self.assertEqual(again["items"][0]["name"], "民族服饰")

        self.assertEqual(len(self.service.list_batches()["batches"]), 1)
        trace = self.service.batch_trace(first["id"])
        registrations = [e for e in trace["entries"] if e["action"] == "登记批次"]
        self.assertEqual(len(registrations), 1)

    def test_request_key_replay_has_no_side_effect(self) -> None:
        batch = self.create_batch()
        item_id = batch["items"][0]["id"]
        self.receive(item_id, 10)
        self.accept(item_id)

        first = self.release(item_id, 4, request_key="release-1")
        replay = self.release(item_id, 4, request_key="release-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])

        item = self.service.get_item(item_id)
        self.assertEqual(item["buckets"]["可用"], 4)
        entries = [e for e in self.service.item_trace(item_id)["entries"] if e["action"] == "放行"]
        self.assertEqual(len(entries), 1)

    # ------------------------------------------------------------------
    # 去向追踪
    # ------------------------------------------------------------------
    def test_full_trace_from_intake_to_disposal(self) -> None:
        batch = self.create_batch()
        batch_id, item_id = batch["id"], batch["items"][0]["id"]
        self.receive(item_id, 10)
        self.accept(item_id)
        self.release(item_id, 6)
        checkout_id = self.checkout(item_id, 4)["checkout"]["id"]
        self.service.return_checkout(checkout_id, 1, "可再用", **TEACHER)
        self.service.return_checkout(checkout_id, 1, "不可再用", **TEACHER)
        self.service.dispose(item_id, 2, "隔离", "受潮霉变", **ADMIN)
        self.service.return_to_donor(item_id, 2, "隔离", "捐赠方召回", **ADMIN)

        item = self.service.get_item(item_id)
        self.assertEqual(
            item["buckets"],
            {"隔离": 0, "可用": 3, "领用": 2, "退回捐赠方": 2, "处置": 3},
        )
        self.assertEqual(item["qty_received"], 10)
        self.assertEqual(item["qty_expected"], 10)

        trace = self.service.batch_trace(batch_id)
        actions = [e["action"] for e in trace["entries"]]
        self.assertEqual(
            actions,
            ["登记批次", "部分接收", "验收登记", "放行", "领用", "归还", "归还报废", "处置", "退回捐赠方"],
        )
        # 每条分录都挂在批次上，可从捐赠入口追踪到最终去向
        self.assertTrue(all(e["batch_id"] == batch_id for e in trace["entries"]))
        checkout_entries = [e for e in trace["entries"] if e["checkout_id"] == checkout_id]
        self.assertEqual(len(checkout_entries), 3)

    # ------------------------------------------------------------------
    # 角色边界与参数校验
    # ------------------------------------------------------------------
    def test_actor_and_role_are_enforced(self) -> None:
        batch = self.create_batch()
        item_id = batch["items"][0]["id"]
        self.receive(item_id, 5)
        self.accept(item_id)

        with self.assertRaises(UnauthorizedError):
            self.service.release(item_id, 1, actor="", role="学校资产管理员")
        with self.assertRaises(UnauthorizedError):
            self.service.release(item_id, 1, actor="某人", role="家长")
        with self.assertRaises(ForbiddenError):
            self.service.release(item_id, 1, **TEACHER)
        with self.assertRaises(ForbiddenError):
            self.service.add_receipt(item_id, 1, **DONOR)
        with self.assertRaises(ForbiddenError):
            self.service.change_restriction(item_id, "无限制", "越权尝试", **TEACHER)

    def test_validation_errors(self) -> None:
        with self.assertRaises(ValidationError):
            self.create_batch(items=[])
        with self.assertRaises(ValidationError):
            self.create_batch(items=[self.item_spec(qty_expected=0)])
        with self.assertRaises(ValidationError):
            self.create_batch(items=[self.item_spec(storage_location="")])
        with self.assertRaises(ValidationError):
            self.create_batch(items=[self.item_spec(usage_restriction="随便用")])
        with self.assertRaises(ValidationError):
            self.create_batch(voucher_no=" ")

        batch = self.create_batch()
        item_id = batch["items"][0]["id"]
        with self.assertRaises(ValidationError):
            self.receive(item_id, 0)
        with self.assertRaises(ValidationError):
            self.receive(item_id, -3)
        with self.assertRaises(ValidationError):
            self.service.add_acceptance(item_id, "材质安全检测", "大概合格", **ADMIN)

    def test_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.get_item(999)
        with self.assertRaises(NotFoundError):
            self.service.get_batch(999)
        with self.assertRaises(NotFoundError):
            self.service.release(999, 1, **ADMIN)
        with self.assertRaises(NotFoundError):
            self.service.return_checkout(999, 1, "可再用", **TEACHER)


if __name__ == "__main__":
    unittest.main()
