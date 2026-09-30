"""HTTP 接口的端到端测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quarantine_service.api import make_server

ADMIN = {"actor": "张管理员", "role": "学校资产管理员"}
TEACHER = {"actor": "李老师", "role": "使用教师"}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = make_server(str(Path(self.tmp.name) / "api.db"), "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _create_batch(self, voucher="PZ-0001", restriction="禁止学生使用") -> dict:
        status, batch = self.call(
            "POST",
            "/api/batches",
            {
                "voucher_no": voucher,
                "donor": "某社会机构",
                "source_note": "社会机构捐赠服饰一批",
                "items": [
                    {
                        "name": "民族服饰",
                        "category": "服饰",
                        "qty_expected": 8,
                        "usage_restriction": restriction,
                        "storage_location": "隔离库A-01",
                    }
                ],
                **ADMIN,
            },
        )
        self.assertEqual(status, 201)
        return batch

    def test_health(self) -> None:
        status, payload = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_full_flow_over_http(self) -> None:
        batch = self._create_batch()
        item_id = batch["items"][0]["id"]

        # 重复凭证：返回原批次，不新建
        status, dup = self.call(
            "POST",
            "/api/batches",
            {"voucher_no": "PZ-0001", "donor": "另一机构", "items": [{"name": "X", "qty_expected": 1, "storage_location": "B-01"}], **ADMIN},
        )
        self.assertEqual(status, 200)
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["id"], batch["id"])

        # 部分接收 → 验收 → 部分放行
        status, _ = self.call("POST", f"/api/items/{item_id}/receipts", {"quantity": 8, **ADMIN})
        self.assertEqual(status, 200)
        status, _ = self.call(
            "POST", f"/api/items/{item_id}/acceptance", {"check_item": "材质安全检测", "result": "合格", **ADMIN}
        )
        self.assertEqual(status, 200)
        status, released = self.call("POST", f"/api/items/{item_id}/release", {"quantity": 5, **ADMIN})
        self.assertEqual(status, 200)
        self.assertEqual(released["item"]["buckets"]["可用"], 5)
        self.assertEqual(released["item"]["buckets"]["隔离"], 3)

        # 可用库存可见，且能追溯到来源批次
        status, inv = self.call("GET", "/api/inventory")
        self.assertEqual(status, 200)
        self.assertEqual(inv["items"][0]["buckets"]["可用"], 5)
        self.assertEqual(inv["items"][0]["voucher_no"], "PZ-0001")

        # 学生领用被用途限制拦截
        status, err = self.call(
            "POST",
            f"/api/items/{item_id}/checkouts",
            {"quantity": 1, "borrower": "李老师", "used_by": "学生", **TEACHER},
        )
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "RESTRICTION_VIOLATION")

        # 教师领用成功
        status, checkout = self.call(
            "POST",
            f"/api/items/{item_id}/checkouts",
            {"quantity": 2, "borrower": "李老师", "used_by": "教师", "purpose": "非遗课程展示", **TEACHER},
        )
        self.assertEqual(status, 201)
        checkout_id = checkout["checkout"]["id"]

        # 归还
        status, returned = self.call(
            "POST", f"/api/checkouts/{checkout_id}/return", {"quantity": 1, "condition": "可再用", **TEACHER}
        )
        self.assertEqual(status, 200)
        self.assertEqual(returned["checkout"]["qty_outstanding"], 1)

        # 从捐赠入口追踪到领用与归还
        status, trace = self.call("GET", f"/api/batches/{batch['id']}/trace")
        self.assertEqual(status, 200)
        actions = [e["action"] for e in trace["entries"]]
        self.assertEqual(actions, ["登记批次", "部分接收", "验收登记", "放行", "领用", "归还"])

        status, ledger = self.call("GET", f"/api/ledger?batch_id={batch['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(ledger["entries"]), 6)

    def test_request_key_replay_over_http(self) -> None:
        batch = self._create_batch(voucher="PZ-0002", restriction="无限制")
        item_id = batch["items"][0]["id"]
        self.call("POST", f"/api/items/{item_id}/receipts", {"quantity": 8, **ADMIN})
        self.call("POST", f"/api/items/{item_id}/acceptance", {"check_item": "材质安全检测", "result": "合格", **ADMIN})
        self.call("POST", f"/api/items/{item_id}/release", {"quantity": 8, **ADMIN})

        body = {"quantity": 1, "borrower": "李老师", "used_by": "教师", "request_key": "ck-http-1", **TEACHER}
        status, first = self.call("POST", f"/api/items/{item_id}/checkouts", body)
        self.assertEqual(status, 201)
        status, replay = self.call("POST", f"/api/items/{item_id}/checkouts", body)
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["checkout"]["id"], replay["checkout"]["id"])

        status, item = self.call("GET", f"/api/items/{item_id}")
        self.assertEqual(item["buckets"]["领用"], 1)

    def test_actor_and_role_enforced_over_http(self) -> None:
        status, err = self.call(
            "POST", "/api/batches", {"voucher_no": "PZ-0003", "donor": "某机构", "items": [{"name": "X", "qty_expected": 1, "storage_location": "B-01"}]}
        )
        self.assertEqual(status, 401)
        self.assertEqual(err["error"]["code"], "UNAUTHORIZED")

        status, err = self.call(
            "POST",
            "/api/batches",
            {"voucher_no": "PZ-0003", "donor": "某机构", "items": [{"name": "X", "qty_expected": 1, "storage_location": "B-01"}], **TEACHER},
        )
        self.assertEqual(status, 403)
        self.assertEqual(err["error"]["code"], "FORBIDDEN")

    def test_unknown_route_and_missing_record(self) -> None:
        status, err = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)
        status, err = self.call("GET", "/api/items/999")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "NOT_FOUND")

    def test_invalid_json_and_validation(self) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/batches",
            data=b"{not-json",
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.fail(f"应返回 400，实际 {resp.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "VALIDATION_ERROR")

        status, err = self.call("POST", "/api/batches", {"voucher_no": "PZ-0004", "donor": "某机构", **ADMIN})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
