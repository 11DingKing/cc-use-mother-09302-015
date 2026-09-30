"""捐赠隔离服务的 HTTP JSON 接口（仅标准库）。

约定：
- 变更请求（POST）在 JSON 体内携带 ``actor`` / ``role``，服务端按角色鉴权；
- 所有变更接口支持可选的 ``request_key`` 幂等键，重复提交返回首个响应；
- 错误统一为 ``{"error": {"code": ..., "message": ...}}``。
"""
from __future__ import annotations

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .errors import DomainError, NotFoundError, ValidationError
from .service import QuarantineService

MAX_BODY_BYTES = 1 << 20


def _int(value: str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是整数") from None


def _first(query: dict, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _build_routes(service: QuarantineService):
    """路由表：(方法, 路径正则, 处理器)。处理器返回 (响应体, 状态码)。"""
    actor = lambda body: (body.get("actor", ""), body.get("role", ""))  # noqa: E731

    def health(body, query):
        return {"status": "ok"}, 200

    def create_batch(body, query):
        who, role = actor(body)
        result = service.create_batch(
            voucher_no=body.get("voucher_no"),
            donor=body.get("donor"),
            source_note=body.get("source_note", ""),
            items=body.get("items"),
            actor=who,
            role=role,
            request_key=body.get("request_key"),
        )
        return result, (200 if result["duplicate"] or result["replayed"] else 201)

    def list_batches(body, query):
        return service.list_batches(), 200

    def get_batch(body, query, batch_id):
        return service.get_batch(_int(batch_id, "批次号")), 200

    def batch_trace(body, query, batch_id):
        return service.batch_trace(_int(batch_id, "批次号")), 200

    def add_receipt(body, query, item_id):
        who, role = actor(body)
        result = service.add_receipt(
            _int(item_id, "物品号"),
            body.get("quantity"),
            actor=who,
            role=role,
            note=body.get("note", ""),
            request_key=body.get("request_key"),
        )
        return result, 200

    def add_acceptance(body, query, item_id):
        who, role = actor(body)
        result = service.add_acceptance(
            _int(item_id, "物品号"),
            body.get("check_item"),
            body.get("result"),
            actor=who,
            role=role,
            note=body.get("note", ""),
            request_key=body.get("request_key"),
        )
        return result, 200

    def release(body, query, item_id):
        who, role = actor(body)
        result = service.release(
            _int(item_id, "物品号"),
            body.get("quantity"),
            actor=who,
            role=role,
            note=body.get("note", ""),
            request_key=body.get("request_key"),
        )
        return result, 200

    def return_to_donor(body, query, item_id):
        who, role = actor(body)
        result = service.return_to_donor(
            _int(item_id, "物品号"),
            body.get("quantity"),
            body.get("from_bucket"),
            body.get("reason"),
            actor=who,
            role=role,
            request_key=body.get("request_key"),
        )
        return result, 200

    def dispose(body, query, item_id):
        who, role = actor(body)
        result = service.dispose(
            _int(item_id, "物品号"),
            body.get("quantity"),
            body.get("from_bucket"),
            body.get("reason"),
            actor=who,
            role=role,
            request_key=body.get("request_key"),
        )
        return result, 200

    def change_restriction(body, query, item_id):
        who, role = actor(body)
        result = service.change_restriction(
            _int(item_id, "物品号"),
            body.get("usage_restriction"),
            body.get("reason"),
            actor=who,
            role=role,
            request_key=body.get("request_key"),
        )
        return result, 200

    def checkout(body, query, item_id):
        who, role = actor(body)
        result = service.checkout(
            _int(item_id, "物品号"),
            body.get("quantity"),
            borrower=body.get("borrower"),
            used_by=body.get("used_by"),
            purpose=body.get("purpose", ""),
            actor=who,
            role=role,
            request_key=body.get("request_key"),
        )
        return result, (200 if result["replayed"] else 201)

    def return_checkout(body, query, checkout_id):
        who, role = actor(body)
        result = service.return_checkout(
            _int(checkout_id, "领用单号"),
            body.get("quantity"),
            body.get("condition"),
            actor=who,
            role=role,
            note=body.get("note", ""),
            request_key=body.get("request_key"),
        )
        return result, 200

    def get_item(body, query, item_id):
        return service.get_item(_int(item_id, "物品号")), 200

    def item_trace(body, query, item_id):
        return service.item_trace(_int(item_id, "物品号")), 200

    def inventory(body, query):
        return service.list_inventory(), 200

    def ledger(body, query):
        batch_id = _first(query, "batch_id")
        item_id = _first(query, "item_id")
        limit = _first(query, "limit")
        return (
            service.list_ledger(
                batch_id=_int(batch_id, "批次号") if batch_id else None,
                item_id=_int(item_id, "物品号") if item_id else None,
                limit=_int(limit, "条数上限") if limit else 200,
            ),
            200,
        )

    return [
        ("GET", re.compile(r"/api/health"), health),
        ("POST", re.compile(r"/api/batches"), create_batch),
        ("GET", re.compile(r"/api/batches"), list_batches),
        ("GET", re.compile(r"/api/batches/(?P<batch_id>\d+)/trace"), batch_trace),
        ("GET", re.compile(r"/api/batches/(?P<batch_id>\d+)"), get_batch),
        ("POST", re.compile(r"/api/items/(?P<item_id>\d+)/receipts"), add_receipt),
        ("POST", re.compile(r"/api/items/(?P<item_id>\d+)/acceptance"), add_acceptance),
        ("POST", re.compile(r"/api/items/(?P<item_id>\d+)/release"), release),
        ("POST", re.compile(r"/api/items/(?P<item_id>\d+)/return-to-donor"), return_to_donor),
        ("POST", re.compile(r"/api/items/(?P<item_id>\d+)/dispose"), dispose),
        ("POST", re.compile(r"/api/items/(?P<item_id>\d+)/restriction"), change_restriction),
        ("POST", re.compile(r"/api/items/(?P<item_id>\d+)/checkouts"), checkout),
        ("POST", re.compile(r"/api/checkouts/(?P<checkout_id>\d+)/return"), return_checkout),
        ("GET", re.compile(r"/api/items/(?P<item_id>\d+)/trace"), item_trace),
        ("GET", re.compile(r"/api/items/(?P<item_id>\d+)"), get_item),
        ("GET", re.compile(r"/api/inventory"), inventory),
        ("GET", re.compile(r"/api/ledger"), ledger),
    ]


def make_handler(service: QuarantineService):
    routes = _build_routes(service)

    class Handler(BaseHTTPRequestHandler):
        server_version = "QuarantineService/0.1"

        def log_message(self, format, *args):  # 保持测试输出干净
            pass

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            try:
                split = urlsplit(self.path)
                query = parse_qs(split.query)
                for route_method, pattern, handler in routes:
                    if route_method != method:
                        continue
                    match = pattern.fullmatch(split.path)
                    if match:
                        body = self._read_body() if method == "POST" else {}
                        payload, status = handler(body=body, query=query, **match.groupdict())
                        self._send(status, payload)
                        return
                raise NotFoundError("接口不存在")
            except DomainError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            except Exception:
                traceback.print_exc()
                self._send(500, {"error": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}})

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValidationError("Content-Length 不合法") from None
            if length > MAX_BODY_BYTES:
                raise ValidationError("请求体过大")
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValidationError("请求体不是合法 JSON") from None
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """构建多线程 HTTP 服务（port=0 时由系统分配端口）。"""
    service = QuarantineService(db_path)
    return ThreadingHTTPServer((host, port), make_handler(service))


def run_server(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> None:
    server = make_server(db_path, host, port)
    actual_host, actual_port = server.server_address[:2]
    print(f"捐赠隔离服务已启动：http://{actual_host}:{actual_port}（数据库：{db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
