"""HTTP 接口端到端冒烟测试（真实线程服务器 + JSON 请求）。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from donation_service.api import make_handler


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "api.db")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_end_to_end_flow(self) -> None:
        status, body = self.call("POST", "/donors", {"name": "锦绣公益基金会"})
        self.assertEqual(status, 200)

        status, body = self.call("POST", "/batches", {
            "batch_no": "B-01", "voucher_no": "V-01",
            "material_name": "苗绣服饰", "category": "服饰",
            "declared_qty": 3, "use_restriction": "限非遗课堂使用",
            "donor_name": "锦绣公益基金会",
            "items": ["A-1", "A-2", "A-3"],
        })
        self.assertEqual(status, 200, body)

        # 重复凭证 → 409
        status, body = self.call("POST", "/batches", {
            "batch_no": "B-02", "voucher_no": "V-01",
            "material_name": "其他", "declared_qty": 1,
            "use_restriction": "x", "donor_name": "锦绣公益基金会",
            "items": ["B-1"],
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "duplicate_voucher")

        # 部分接收
        status, _ = self.call("POST", "/batches/B-01/receive", {
            "qty": 3, "ref_no": "R-1", "location": "隔离柜 1",
        })
        self.assertEqual(status, 200)

        # 隔离期间领用 → 409（事故场景回归）
        status, body = self.call("POST", "/issues", {
            "teacher": "王老师", "item_codes": ["A-1"], "ref_no": "I-0",
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "quarantine_control")

        # 验收批准 2 件、放行
        self.call("POST", "/batches/B-01/inspect", {
            "decisions": {"A-1": True, "A-2": True, "A-3": False},
            "checks": [{"check_name": "面料安全", "result": "PASS"}],
        })
        status, _ = self.call("POST", "/batches/B-01/release", {
            "qty": 2, "ref_no": "REL-1", "location": "可用库房",
        })
        self.assertEqual(status, 200)

        # 领用一件并追踪
        status, _ = self.call("POST", "/issues", {
            "teacher": "王老师", "item_codes": ["A-1"], "ref_no": "I-1",
            "purpose": "体验课",
        })
        self.assertEqual(status, 200)
        status, body = self.call("GET", "/items/A-1/trace")
        self.assertEqual(status, 200)
        actions = [e["action"] for e in body["data"]["trace"]]
        self.assertEqual(actions, ["登记", "接收", "验收批准", "放行", "领用"])
        self.assertEqual(body["data"]["state"], "领用")

        # 不存在的路由
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
