"""HTTP 接口层（仅用标准库 ``http.server``）。

把领域服务暴露为 JSON 接口；``DomainError`` 统一映射为带 ``error.code`` 的
4xx 响应，所有写操作在服务层内以单事务提交，因此接口层不做任何部分提交。
"""
from __future__ import annotations

import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import unquote

from .database import connect, initialize_database
from .errors import DomainError, NotFoundError, ValidationError
from .services import Service


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), handler))

    def match(self, method: str, path: str):
        for m, regex, handler in self.routes:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                return handler, match.groupdict()
        return None, {}


def build_router() -> Router:
    router = Router()

    router.add("POST", r"/donors", lambda svc, p, b: svc.register_donor(**b))
    router.add("GET", r"/donors", lambda svc, p, b: svc.list_donors())

    router.add("POST", r"/batches", lambda svc, p, b: svc.register_batch(**b))
    router.add("GET", r"/batches", lambda svc, p, b: svc.list_batches())
    router.add("GET", r"/batches/(?P<batch_no>[^/]+)", lambda svc, p, b: svc.get_batch(batch_no=p["batch_no"]))

    router.add(
        "POST",
        r"/batches/(?P<batch_no>[^/]+)/receive",
        lambda svc, p, b: svc.receive_items(batch_no=p["batch_no"], **b),
    )
    router.add(
        "POST",
        r"/batches/(?P<batch_no>[^/]+)/inspect",
        lambda svc, p, b: svc.inspect_batch(batch_no=p["batch_no"], **b),
    )
    router.add(
        "POST",
        r"/batches/(?P<batch_no>[^/]+)/release",
        lambda svc, p, b: svc.release_items(batch_no=p["batch_no"], **b),
    )
    router.add(
        "POST",
        r"/batches/(?P<batch_no>[^/]+)/restriction",
        lambda svc, p, b: svc.change_restriction(batch_no=p["batch_no"], **b),
    )
    router.add(
        "POST",
        r"/batches/(?P<batch_no>[^/]+)/return",
        lambda svc, p, b: svc.return_items(batch_no=p["batch_no"], **b),
    )
    router.add(
        "POST",
        r"/batches/(?P<batch_no>[^/]+)/dispose",
        lambda svc, p, b: svc.dispose_items(batch_no=p["batch_no"], **b),
    )

    router.add("POST", r"/issues", lambda svc, p, b: svc.issue_items(**b))
    router.add("GET", r"/issues", lambda svc, p, b: svc.list_active_issues(**(p or {})))
    router.add("POST", r"/returns", lambda svc, p, b: svc.return_issued_items(**b))

    router.add(
        "GET",
        r"/items/(?P<item_code>[^/]+)/trace",
        lambda svc, p, b: svc.get_item_trace(unquote(p["item_code"])),
    )
    return router


def make_handler(database: str) -> type[BaseHTTPRequestHandler]:
    router = build_router()

    class Handler(BaseHTTPRequestHandler):
        server_version = "DonationQuarantine/1.0"

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return value

        def _handle(self, method: str) -> None:
            # 每个请求独立连接，SQLite 写事务串行化保证并发安全
            conn = connect(database)
            try:
                initialize_database(conn)
                service = Service(conn)
                handler, params = router.match(method, self.path.split("?", 1)[0])
                if handler is None:
                    self._send(404, {"error": {"code": "not_found", "message": "接口不存在"}})
                    return
                body = self._read_json() if method == "POST" else {}
                query = self._query_params() if method == "GET" else {}
                try:
                    result = handler(service, params or query, body)
                    self._send(200, {"ok": True, "data": result})
                except DomainError as exc:
                    self._send(
                        exc.http_status,
                        {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
                    )
            finally:
                conn.close()

        def _query_params(self) -> dict[str, str]:
            if "?" not in self.path:
                return {}
            from urllib.parse import parse_qs

            parsed = parse_qs(self.path.split("?", 1)[1])
            return {k: v[0] for k, v in parsed.items()}

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

    return Handler


def run(database: str = "donations.db", host: str = "127.0.0.1", port: int = 8000) -> None:
    # 启动即确保表结构就绪
    conn = connect(database)
    try:
        initialize_database(conn)
    finally:
        conn.close()
    server = ThreadingHTTPServer((host, port), make_handler(database))
    print(f"捐赠隔离服务已启动：http://{host}:{port}（数据库 {database}）")
    server.serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="非遗材料捐赠隔离服务端")
    parser.add_argument("--db", default="donations.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run(args.db, args.host, args.port)
