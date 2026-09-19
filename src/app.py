"""HTTP 入口：标准库实现的 JSON API。

业务事实全部写入 .runtime/events.jsonl；响应一律 JSON。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from domain import ConflictError, DomainError, NotFoundError
from ledger import Ledger
from reports import Reports
from projection import Service

SERVICE_NAME = '健身教练绩效分润平台'

_runtime_lock = threading.Lock()
_runtime: dict[str, Any] = {}


def get_runtime() -> dict[str, Any]:
    """惰性初始化，便于测试用独立目录构造后替换。"""
    if not _runtime:
        with _runtime_lock:
            if not _runtime:
                ledger = Ledger(os.getenv("RUNTIME_DIR", ".runtime"))
                _runtime["ledger"] = ledger
                _runtime["service"] = Service(ledger)
                _runtime["reports"] = Reports(ledger)
    return _runtime


def configure(ledger: Ledger) -> None:
    """测试入口：注入指定目录的台账。"""
    with _runtime_lock:
        _runtime["ledger"] = ledger
        _runtime["service"] = Service(ledger)
        _runtime["reports"] = Reports(ledger)


def reset_runtime() -> None:
    with _runtime_lock:
        _runtime.clear()


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# ------------------------------------------------------------- 路由表
def _err(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": code, "message": message}


def build_routes() -> list[tuple[str, str, Callable[..., Any]]]:
    # (方法, 路径前缀, 处理函数)
    return [
        ("POST", "/stores", lambda s, b, q: s.register_store(b["name"], b.get("store_id"))),
        ("POST", "/people", lambda s, b, q: s.register_person(
            b["name"], b["role"], b.get("person_id"), b.get("store_id"))),
        ("POST", "/rules", lambda s, b, q: s.publish_rule(
            b["effective_from"], b["commission_rate"], b["referral_rate"],
            b.get("referral_qualifying_min_cents", 0), b.get("version_id"))),
        ("POST", "/sales", lambda s, b, q: s.sell_package(b)),
        ("POST", "/sessions", lambda s, b, q: s.create_session(b)),
        ("POST", "/substitutions", lambda s, b, q: s.substitute_coach(
            b["session_id"], b["new_coach_id"], b["reason"])),
        ("POST", "/bookings", lambda s, b, q: s.create_booking(b)),
        ("POST", "/checkins", lambda s, b, q: s.check_in(b)),
        ("POST", "/checkin-reversals", lambda s, b, q: s.reverse_checkin(
            b["checkin_id"], b["reason"], b.get("correction_batch_id"))),
        ("POST", "/transfers", lambda s, b, q: s.transfer_package(
            b["sale_id"], b["new_member_id"], b.get("to_store_id"))),
        ("POST", "/refunds", lambda s, b, q: s.refund(b)),
        ("POST", "/bad-debts", lambda s, b, q: s.bad_debt(b)),
        ("POST", "/settlements/lock", lambda s, b, q: s.lock_settlement(
            b["period"], b.get("currency", "CNY"))),
        ("POST", "/corrections/open", lambda s, b, q: s.open_correction(
            b["period"], b["reason"])),
        ("POST", "/corrections/post", lambda s, b, q: s.post_correction(
            b["correction_batch_id"], b.get("adjustments"), b.get("request_id"))),
    ]


def handle_api(method: str, path: str, body: dict[str, Any], query: dict[str, list[str]]) -> Any:
    rt = get_runtime()
    service: Service = rt["service"]
    reports: Reports = rt["reports"]

    if method == "GET" and path == "/health":
        return 200, health_payload()

    if method == "GET" and path.startswith("/people/") and path.endswith("/earnings"):
        person_id = path.split("/")[2]
        period = query.get("period", [None])[0]
        include_corr = query.get("include_corrections", ["1"])[0] not in ("0", "false")
        return 200, reports.coach_earnings(person_id, period, include_corr)

    if method == "GET" and path == "/stores/comparison":
        period = query.get("period", [None])[0]
        if not period:
            return _err(400, "bad_request", "缺少 period 查询参数（YYYY-MM）")
        return 200, reports.store_comparison(period)

    if method == "GET" and path.startswith("/settlements/"):
        period = path.split("/")[2]
        currency = query.get("currency", ["CNY"])[0]
        return 200, reports.settlement_batch(period, currency)

    if method == "GET" and path.startswith("/corrections/"):
        batch_id = path.split("/")[2]
        return 200, reports.correction_batch(batch_id)

    for m, prefix, fn in build_routes():
        if method == m and path == prefix:
            return 200, fn(service, body, query)

    return _err(404, "not_found", f"无此接口: {method} {path}")


class RequestHandler(BaseHTTPRequestHandler):
    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        body: dict[str, Any] = {}
        if method in ("POST", "PUT", "PATCH"):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if raw:
                try:
                    parsed_body = json.loads(raw.decode("utf-8"))
                    if not isinstance(parsed_body, dict):
                        raise ValueError
                    body = parsed_body
                except (ValueError, UnicodeDecodeError):
                    self._write_json(400, {"error": "bad_json", "message": "请求体必须是 JSON 对象"})
                    return
        try:
            status, payload = handle_api(
                method, parsed.path, body, parse_qs(parsed.query)
            )
        except NotFoundError as exc:
            status, payload = 404, {"error": "not_found", "message": str(exc)}
        except ConflictError as exc:
            status, payload = 409, {"error": "conflict", "message": str(exc)}
        except DomainError as exc:
            status, payload = 400, {"error": "bad_request", "message": str(exc)}
        self._write_json(status, payload)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RequestHandler)
