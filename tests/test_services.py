"""捐赠隔离领域服务的完整回归测试。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from donation_service import (
    Accounts,
    DomainError,
    Service,
    connect,
    initialize_database,
)
from donation_service.accounts import TxnType
from donation_service.errors import (
    DuplicateReferenceError,
    DuplicateVoucherError,
    InsufficientBalanceError,
    QuarantineControlError,
)


def build_service(database: str = ":memory:"):
    conn = connect(database)
    initialize_database(conn)
    svc = Service(conn)
    svc.register_donor("锦绣公益基金会", "李老师 13800000000")
    svc.register_batch(
        batch_no="B2026-001",
        voucher_no="JZ-2026-09-001",
        material_name="苗绣服饰",
        category="服饰",
        declared_qty=5,
        use_restriction="仅限非遗课堂学生使用，不得外借",
        donor_name="锦绣公益基金会",
        items=[
            {"item_code": "FS-001", "detail": "女童上衣 S"},
            {"item_code": "FS-002", "detail": "女童上衣 M"},
            {"item_code": "FS-003", "detail": "男童上衣 M"},
            {"item_code": "FS-004", "detail": "长裙"},
            {"item_code": "FS-005", "detail": "配饰腰带"},
        ],
        registered_by="资产管理员",
    )
    return conn, svc


def make_three_available(svc: Service) -> None:
    svc.receive_items(batch_no="B2026-001", qty=5, ref_no="RCV-ALL", location="一号隔离柜")
    svc.inspect_batch(batch_no="B2026-001", qty_approved=3)
    svc.release_items(batch_no="B2026-001", qty=3, ref_no="REL-1", location="可用库房 A")


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.svc = build_service()

    def tearDown(self) -> None:
        self.conn.close()

    # ---------- 登记 ----------

    def test_register_creates_pending_balances(self) -> None:
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.PENDING), 5)
        self.assertEqual(batch["received_qty"], 0)
        self.assertEqual(len(batch["items"]), 5)
        for item in batch["items"]:
            self.assertEqual(item["state"], "登记")

    def test_duplicate_voucher_rejected(self) -> None:
        with self.assertRaises(DuplicateVoucherError):
            self.svc.register_batch(
                batch_no="B2026-002",
                voucher_no="JZ-2026-09-001",  # 同一凭证号
                material_name="其他服饰",
                declared_qty=1,
                use_restriction="限校内",
                donor_name="锦绣公益基金会",
                items=["X-1"],
            )
        # 被拒绝后不得有任何残留
        self.assertEqual(len(self.svc.list_batches()), 1)

    def test_items_count_must_match_declared(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.register_batch(
                batch_no="B2026-003",
                voucher_no="JZ-2026-09-003",
                material_name="围兜",
                declared_qty=3,
                use_restriction="限校内",
                donor_name="锦绣公益基金会",
                items=["W-1", "W-2"],
            )

    # ---------- 接收（部分接收） ----------

    def test_partial_receive(self) -> None:
        # 第一次只接收 3 件
        self.svc.receive_items(
            batch_no="B2026-001", qty=3, ref_no="RCV-1",
            location="一号隔离柜", received_by="仓管",
        )
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.QUARANTINE), 3)
        self.assertEqual(batch["balances"].get(Accounts.PENDING), 2)
        self.assertEqual(batch["received_qty"], 3)

        # 剩余 2 件后续接收
        self.svc.receive_items(
            batch_no="B2026-001", qty=2, ref_no="RCV-2",
            location="一号隔离柜", received_by="仓管",
        )
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.QUARANTINE), 5)
        self.assertIsNone(batch["balances"].get(Accounts.PENDING))

    def test_duplicate_receive_reference_rejected(self) -> None:
        self.svc.receive_items(
            batch_no="B2026-001", qty=1, ref_no="RCV-DUP",
            location="一号隔离柜",
        )
        with self.assertRaises(DuplicateReferenceError):
            self.svc.receive_items(
                batch_no="B2026-001", qty=1, ref_no="RCV-DUP",
                location="一号隔离柜",
            )
        # 重复单被整体回滚：只接收了 1 件
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.QUARANTINE), 1)

    # ---------- 验收与放行 ----------

    def _receive_all(self) -> None:
        self.svc.receive_items(
            batch_no="B2026-001", qty=5, ref_no="RCV-ALL", location="一号隔离柜"
        )

    def test_quarantine_items_cannot_be_issued(self) -> None:
        """核心事故回归：验收完成前隔离物品不得被教师领走。"""
        self._receive_all()
        with self.assertRaises(QuarantineControlError):
            self.svc.issue_items(
                teacher="王老师", item_codes=["FS-001"], ref_no="ISS-1"
            )

    def test_only_approved_qty_can_be_released(self) -> None:
        self._receive_all()
        # 验收：3 件合格、2 件不合格（材料不适合学生）
        self.svc.inspect_batch(
            batch_no="B2026-001",
            decisions={"FS-001": True, "FS-002": True, "FS-003": True,
                       "FS-004": False, "FS-005": False},
            checks=[
                {"check_name": "面料安全性", "result": "PASS"},
                {"check_name": "尺寸适配", "result": "FAIL", "note": "FS-004/005 尺寸不适合学生"},
                {"check_name": "卫生状况", "result": "PASS"},
            ],
            basis="GB 学生用品安全要求",
            inspector="验收小组",
        )
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.APPROVED), 3)
        self.assertEqual(batch["balances"].get(Accounts.REJECTED), 2)

        # 未放行时批准件仍不能领用（必须先转入可用库存）
        with self.assertRaises(QuarantineControlError):
            self.svc.issue_items(teacher="王老师", item_codes=["FS-001"], ref_no="ISS-EARLY")

        # 只能放行批准的 3 件；试图放行拒收件应被拒绝
        with self.assertRaises(QuarantineControlError):
            self.svc.release_items(
                batch_no="B2026-001", item_codes=["FS-004"],
                location="可用库房 A",
            )
        self.svc.release_items(
            batch_no="B2026-001", qty=3, ref_no="REL-1", location="可用库房 A"
        )
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.AVAILABLE), 3)
        self.assertIsNone(batch["balances"].get(Accounts.APPROVED))
        self.assertEqual(batch["balances"].get(Accounts.REJECTED), 2)

    def test_release_exceeding_approved_qty_rejected(self) -> None:
        self._receive_all()
        self.svc.inspect_batch(batch_no="B2026-001", qty_approved=2)
        with self.assertRaises(DomainError):
            self.svc.release_items(batch_no="B2026-001", qty=3, location="可用库房 A")

    # ---------- 退回与处置 ----------

    def test_rejected_items_can_be_returned(self) -> None:
        self._receive_all()
        self.svc.inspect_batch(batch_no="B2026-001", qty_approved=3)
        self.svc.release_items(batch_no="B2026-001", qty=3, ref_no="R1", location="可用库房 A")
        rejected = ["FS-004", "FS-005"]
        self.svc.return_items(
            batch_no="B2026-001", item_codes=rejected,
            reason="材料不适合学生使用，协商退回", ref_no="RET-1",
        )
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.RETURNED), 2)
        self.assertIsNone(batch["balances"].get(Accounts.REJECTED))

    def test_approved_cannot_be_returned_but_available_can_dispose(self) -> None:
        self._receive_all()
        self.svc.inspect_batch(batch_no="B2026-001", qty_approved=4)
        # 批准待放行的物品不能走退回（必须尊重验收结论）
        with self.assertRaises(QuarantineControlError):
            self.svc.return_items(
                batch_no="B2026-001", item_codes=["FS-001"], reason="误操作"
            )
        self.svc.release_items(batch_no="B2026-001", qty=4, ref_no="R1", location="可用库房 A")
        # 放行后发现问题，可处置（从可用库存核销）
        self.svc.dispose_items(
            batch_no="B2026-001", item_codes=["FS-001"],
            reason="入库复检发现面料脱色", method="无害化销毁", ref_no="DSP-1",
        )
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.DISPOSED), 1)
        self.assertEqual(batch["balances"].get(Accounts.AVAILABLE), 3)

    def test_dispose_requires_method(self) -> None:
        self._receive_all()
        with self.assertRaises(DomainError):
            self.svc.dispose_items(
                batch_no="B2026-001", item_codes=["FS-001"], reason="x"
            )

    # ---------- 限制变更 ----------

    def test_restriction_change_is_versioned_and_traced(self) -> None:
        self.svc.change_restriction(
            batch_no="B2026-001", restriction="仅限教师演示使用，禁止学生直接穿戴",
            reason="捐赠方补充安全告知", changed_by="资产管理员",
        )
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["use_restriction"], "仅限教师演示使用，禁止学生直接穿戴")
        self.assertEqual(len(batch["restrictions"]), 2)
        self.assertEqual(batch["version"], 1)
        # 实物数量不变
        self.assertEqual(batch["balances"].get(Accounts.PENDING), 5)
        trace = self.svc.get_item_trace("FS-001")
        actions = [e["action"] for e in trace["trace"]]
        self.assertIn("限制变更", actions)

    # ---------- 领用与归还 ----------

    def _make_three_available(self) -> None:
        self._receive_all()
        self.svc.inspect_batch(batch_no="B2026-001", qty_approved=3)
        self.svc.release_items(batch_no="B2026-001", qty=3, ref_no="REL-1", location="可用库房 A")

    def test_issue_and_give_back_reuse(self) -> None:
        self._make_three_available()
        self.svc.issue_items(
            teacher="王老师", item_codes=["FS-001", "FS-002"],
            ref_no="ISS-100", purpose="苗绣体验课",
        )
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.ISSUED), 2)
        self.assertEqual(batch["balances"].get(Accounts.AVAILABLE), 1)

        # 同一件不能重复领用
        with self.assertRaises(DomainError):
            self.svc.issue_items(teacher="李老师", item_codes=["FS-001"], ref_no="ISS-101")

        # 归还一件重新入库
        self.svc.return_issued_items(teacher="王老师", item_codes=["FS-001"], outcome="REUSE")
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.AVAILABLE), 2)
        self.assertEqual(batch["balances"].get(Accounts.ISSUED), 1)

    def test_give_back_discard_ends_in_disposal(self) -> None:
        self._make_three_available()
        self.svc.issue_items(teacher="王老师", item_codes=["FS-001"], ref_no="ISS-200")
        self.svc.return_issued_items(
            teacher="王老师", item_codes=["FS-001"],
            outcome="DISCARD", reason="使用中发现脱线，无法再用",
        )
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.DISPOSED), 1)
        self.assertIsNone(batch["balances"].get(Accounts.ISSUED))

    def test_only_issuing_teacher_may_return(self) -> None:
        self._make_three_available()
        self.svc.issue_items(teacher="王老师", item_codes=["FS-001"], ref_no="ISS-300")
        with self.assertRaises(QuarantineControlError):
            self.svc.return_issued_items(teacher="赵老师", item_codes=["FS-001"])

    def test_duplicate_issue_reference_rejected(self) -> None:
        self._make_three_available()
        self.svc.issue_items(teacher="王老师", qty=1, batch_no="B2026-001", ref_no="ISS-DUP")
        with self.assertRaises(DuplicateReferenceError):
            self.svc.issue_items(teacher="王老师", qty=1, batch_no="B2026-001", ref_no="ISS-DUP")
        batch = self.svc.get_batch(batch_no="B2026-001")
        self.assertEqual(batch["balances"].get(Accounts.ISSUED), 1)

    def test_concurrent_issue_same_item_only_one_wins(self) -> None:
        """并发领用：同一件物品两个线程抢，恰好一个成功、一个余额不足。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "conc.db")
            conn0, svc0 = build_service(db_path)
            make_three_available(svc0)  # FS-001/002/003 可用
            conn0.close()

            barrier = threading.Barrier(2)
            errors: list[Exception] = []

            def contest(item: str, ref: str) -> None:
                conn = connect(db_path)
                try:
                    svc = Service(conn)
                    barrier.wait()
                    svc.issue_items(teacher=f"老师-{ref}", item_codes=[item], ref_no=ref)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    conn.close()

            threads = [
                threading.Thread(target=contest, args=("FS-001", "C-1")),
                threading.Thread(target=contest, args=("FS-001", "C-2")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            barrier.abort()

            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], (InsufficientBalanceError, DomainError))
            conn = connect(db_path)
            try:
                batch = Service(conn).get_batch(batch_no="B2026-001")
                self.assertEqual(batch["balances"].get(Accounts.ISSUED), 1)
                self.assertEqual(batch["balances"].get(Accounts.AVAILABLE), 2)
                self.assertEqual(len(Service(conn).list_active_issues()), 1)
            finally:
                conn.close()

    def test_concurrent_qty_issue_does_not_oversell(self) -> None:
        """并发按数量领用同一批次，总成功数绝不超过可用库存。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "conc2.db")
            conn0, svc0 = build_service(db_path)
            make_three_available(svc0)
            conn0.close()

            barrier = threading.Barrier(3)
            errors: list[Exception] = []

            def contest(ref: str) -> None:
                conn = connect(db_path)
                try:
                    svc = Service(conn)
                    barrier.wait()
                    svc.issue_items(
                        teacher=f"老师-{ref}", qty=2, batch_no="B2026-001", ref_no=ref
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    conn.close()

            threads = [threading.Thread(target=contest, args=(f"Q-{i}",)) for i in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            barrier.abort()

            # 库存 3 件，每人要 2 件：最多一人成功
            successes = 3 - len(errors)
            self.assertLessEqual(successes, 1)
            conn = connect(db_path)
            try:
                batch = Service(conn).get_batch(batch_no="B2026-001")
                issued = batch["balances"].get(Accounts.ISSUED, 0)
                available = batch["balances"].get(Accounts.AVAILABLE, 0)
                self.assertEqual(issued + available, 3)
            finally:
                conn.close()

    # ---------- 全链路追踪与台账不变量 ----------

    def test_full_trace_from_entry_to_outcome(self) -> None:
        self._receive_all()
        self.svc.inspect_batch(batch_no="B2026-001", qty_approved=3)
        self.svc.release_items(batch_no="B2026-001", qty=3, ref_no="REL-1", location="可用库房 A")
        self.svc.issue_items(teacher="王老师", item_codes=["FS-001"], ref_no="ISS-9")
        self.svc.return_issued_items(teacher="王老师", item_codes=["FS-001"], outcome="REUSE")

        trace = self.svc.get_item_trace("FS-001")
        actions = [e["action"] for e in trace["trace"]]
        self.assertEqual(
            actions, ["登记", "接收", "验收批准", "放行", "领用", "归还入库"]
        )
        self.assertEqual(trace["batch_no"], "B2026-001")
        self.assertEqual(trace["voucher_no"], "JZ-2026-09-001")
        self.assertEqual(trace["state"], "可用")
        # 最新有位置的事件即当前保管位置
        self.assertEqual(trace["location"], "可用库房")

    def test_rejected_item_trace_ends_returned(self) -> None:
        self._receive_all()
        self.svc.inspect_batch(batch_no="B2026-001", qty_approved=4)
        self.svc.return_items(
            batch_no="B2026-001", item_codes=["FS-005"],
            reason="不适合学生", ref_no="RET-X",
        )
        trace = self.svc.get_item_trace("FS-005")
        self.assertEqual(trace["state"], "退回")
        self.assertEqual([e["action"] for e in trace["trace"]][-1], "退回捐赠方")

    def test_ledger_entries_always_balance(self) -> None:
        """每笔事务分录之和为 0，且所有实物账户、每件物品均无负余额。"""
        self._receive_all()
        self.svc.inspect_batch(batch_no="B2026-001", qty_approved=3)
        self.svc.release_items(batch_no="B2026-001", qty=3, ref_no="REL-1", location="可用库房 A")
        self.svc.issue_items(teacher="王老师", item_codes=["FS-001"], ref_no="ISS-1")
        self.svc.return_items(
            batch_no="B2026-001", item_codes=["FS-004", "FS-005"],
            reason="退回", ref_no="RET-1",
        )

        rows = self.conn.execute(
            "SELECT txn_id, SUM(delta) AS s FROM ledger_entries GROUP BY txn_id"
        ).fetchall()
        for row in rows:
            self.assertEqual(row["s"], 0)

        rows = self.conn.execute(
            """
            SELECT account, item_code, SUM(delta) AS qty
            FROM ledger_entries
            WHERE account != '捐赠来源'
            GROUP BY account, item_code
            """
        ).fetchall()
        for row in rows:
            self.assertGreaterEqual(row["qty"], 0)

        # 每件物品的实物账户余额合计恒为 1（登记数量守恒）
        rows = self.conn.execute(
            """
            SELECT item_code, SUM(delta) AS qty
            FROM ledger_entries WHERE account != '捐赠来源'
            GROUP BY item_code
            """
        ).fetchall()
        for row in rows:
            self.assertEqual(row["qty"], 1)

        # 捐赠来源合计 = -5，与申报数量一致
        source = self.conn.execute(
            "SELECT COALESCE(SUM(delta),0) AS s FROM ledger_entries WHERE account='捐赠来源'"
        ).fetchone()["s"]
        self.assertEqual(source, -5)

    def test_transaction_entries_are_queryable(self) -> None:
        self._receive_all()
        batch = self.svc.get_batch(batch_no="B2026-001")
        txn_ids = [t["id"] for t in batch["transactions"] if t["txn_type"] == TxnType.RECEIVE]
        self.assertEqual(len(txn_ids), 1)
        detail = self.svc.get_transaction(txn_ids[0])
        self.assertEqual(len(detail["entries"]), 10)  # 5 件 × 借贷 2 条
        self.assertEqual(detail["ref_no"], "RCV-ALL")


if __name__ == "__main__":
    unittest.main()
