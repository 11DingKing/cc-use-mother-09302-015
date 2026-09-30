"""并发领用与幂等键竞争的测试。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quarantine_service.errors import ConflictError
from quarantine_service.service import QuarantineService

ADMIN = {"actor": "张管理员", "role": "学校资产管理员"}
USABLE = 20


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = QuarantineService(str(Path(self.tmp.name) / "concurrent.db"))
        batch = self.service.create_batch(
            voucher_no="PZ-CONCURRENT",
            donor="某社会机构",
            items=[
                {
                    "name": "民族服饰",
                    "category": "服饰",
                    "qty_expected": USABLE,
                    "usage_restriction": "无限制",
                    "storage_location": "隔离库A-01",
                }
            ],
            **ADMIN,
        )
        self.item_id = batch["items"][0]["id"]
        self.service.add_receipt(self.item_id, USABLE, **ADMIN)
        self.service.add_acceptance(self.item_id, "材质安全检测", "合格", **ADMIN)
        self.service.release(self.item_id, USABLE, **ADMIN)

    def _run_threads(self, count, target) -> None:
        start = threading.Event()
        threads = [threading.Thread(target=target, args=(start, i)) for i in range(count)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join()

    def test_concurrent_checkouts_never_oversell(self) -> None:
        workers = 50
        successes, failures = [], []
        lock = threading.Lock()

        def worker(start, i):
            start.wait()
            try:
                self.service.checkout(
                    self.item_id,
                    1,
                    borrower=f"教师{i}",
                    used_by="教师",
                    actor=f"教师{i}",
                    role="使用教师",
                    request_key=f"ck-{i}",
                )
                with lock:
                    successes.append(i)
            except ConflictError as exc:
                with lock:
                    failures.append(exc.code)

        self._run_threads(workers, worker)

        self.assertEqual(len(successes), USABLE)
        self.assertEqual(len(failures), workers - USABLE)
        self.assertTrue(all(code == "INSUFFICIENT_USABLE" for code in failures))

        item = self.service.get_item(self.item_id)
        self.assertEqual(item["buckets"]["可用"], 0)
        self.assertEqual(item["buckets"]["领用"], USABLE)
        entries = [e for e in self.service.item_trace(self.item_id)["entries"] if e["action"] == "领用"]
        self.assertEqual(len(entries), USABLE)

    def test_same_request_key_settles_exactly_once(self) -> None:
        results, errors = [], []
        lock = threading.Lock()

        def worker(start, _):
            start.wait()
            try:
                result = self.service.checkout(
                    self.item_id,
                    1,
                    borrower="李老师",
                    used_by="教师",
                    actor="李老师",
                    role="使用教师",
                    request_key="race-1",
                )
                with lock:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001 - 测试中收集所有异常
                with lock:
                    errors.append(exc)

        self._run_threads(8, worker)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len({r["checkout"]["id"] for r in results}), 1)
        self.assertEqual(sum(1 for r in results if not r["replayed"]), 1)

        item = self.service.get_item(self.item_id)
        self.assertEqual(item["buckets"]["领用"], 1)
        self.assertEqual(item["buckets"]["可用"], USABLE - 1)


if __name__ == "__main__":
    unittest.main()
