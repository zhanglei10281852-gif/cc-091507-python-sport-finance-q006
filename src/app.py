"""HTTP 入口（标准库，无第三方依赖）。

除 GET /health 外，所有业务接口都是 JSON：
- 写操作 POST 对应资源，请求体即事件负载；
- 查询 GET 个人台账、门店对比、批次与核验。

持久化文件由环境变量 PROFITSHARE_DB 指定，默认 .runtime/events.json。
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from profitshare.engine import DomainError
from profitshare.repository import EventConflictError
from profitshare.rules import RuleVersion
from profitshare.service import Platform

SERVICE_NAME = '健身教练绩效分润平台'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


_PLATFORM_LOCK = threading.RLock()
_PLATFORM: Platform | None = None


def get_platform() -> Platform:
    global _PLATFORM
    with _PLATFORM_LOCK:
        if _PLATFORM is None:
            db_path = os.getenv("PROFITSHARE_DB", ".runtime/events.json")
            _PLATFORM = Platform(Path(db_path))
        return _PLATFORM


def reset_platform(path: str | os.PathLike[str] | None = None) -> Platform:
    """测试用：替换全局平台实例（path=None 表示纯内存）。"""
    global _PLATFORM
    with _PLATFORM_LOCK:
        _PLATFORM = Platform(path)
        return _PLATFORM


def _require_batch(pf: Platform, batch_no: str) -> dict:
    result = pf.batch(batch_no)
    if result is None:
        raise BatchNotFound(batch_no)
    return result


class BatchNotFound(Exception):
    def __init__(self, batch_no: str) -> None:
        super().__init__(batch_no)


def _publish_rule(pf: Platform, body: dict) -> dict:
    rule = RuleVersion(
        version_id=body["version_id"],
        effective_from=body["effective_from"],
        coach_bps=int(body.get("coach_bps", 0)),
        referral_reward_cents=int(body.get("referral_reward_cents", 0)),
        referral_on_checkin=bool(body.get("referral_on_checkin", True)),
        clawback_consumed_refund=bool(
            body.get("clawback_consumed_refund", True)
        ),
        referral_clawback_on_bad_debt=bool(
            body.get("referral_clawback_on_bad_debt", True)
        ),
    )
    event = pf.publish_rule(rule)
    return {"event_id": event.event_id}


# (方法, 正则, 处理器) 处理器返回 (状态码, 对象)
def _routes(pf: Platform):
    def settle(body):
        event = pf.settle_month(body["period"])
        view = pf.rebuild_view()
        batch = view.settlements[body["period"]][0]
        return {"event_id": event.event_id, "batch_no": batch.batch_no}

    return [
        ("POST", re.compile(r"^/rules$"), lambda b: _publish_rule(pf, b)),
        ("POST", re.compile(r"^/packages$"), lambda b: {"event_id": pf.purchase_package(
            package_id=b["package_id"], member_id=b["member_id"], store_id=b["store_id"],
            total_qty=b.get("total_qty", 0), total_amount_cents=b.get("total_amount_cents", 0),
            currency=b.get("currency", "CNY"), sales_owner_id=b.get("sales_owner_id"),
            sold_at=b["sold_at"]).event_id}),
        ("POST", re.compile(r"^/sessions$"), lambda b: {"event_id": pf.schedule_session(
            session_id=b["session_id"], store_id=b["store_id"], coach_id=b["coach_id"],
            start_time=b["start_time"], class_type=b.get("class_type", "group")).event_id}),
        ("POST", re.compile(r"^/substitutes$"), lambda b: {"event_id": pf.register_substitute(
            session_id=b["session_id"], original_coach_id=b["original_coach_id"],
            substitute_coach_id=b["substitute_coach_id"], reason=b["reason"],
            registered_at=b["registered_at"]).event_id}),
        ("POST", re.compile(r"^/bookings$"), lambda b: {"event_id": pf.book_class(
            booking_id=b["booking_id"], session_id=b["session_id"], member_id=b["member_id"],
            package_id=b["package_id"], qty=b.get("qty", 1), booked_at=b["booked_at"]).event_id}),
        ("POST", re.compile(r"^/checkins$"), lambda b: {"event_id": pf.check_in(
            booking_id=b["booking_id"], unit_amount_cents=b.get("unit_amount_cents"),
            package_total_amount_cents=b.get("package_total_amount_cents"),
            service_time=b.get("service_time"), recorded_at=b["recorded_at"]).event_id}),
        ("POST", re.compile(r"^/referrals$"), lambda b: {"event_id": pf.register_referral(
            referral_id=b["referral_id"], referred_member_id=b["referred_member_id"],
            sales_owner_id=b["sales_owner_id"], store_id=b["store_id"],
            occurred_at=b["occurred_at"], currency=b.get("currency", "CNY")).event_id}),
        ("POST", re.compile(r"^/refunds$"), lambda b: {"event_id": pf.refund(
            refund_id=b["refund_id"], package_id=b["package_id"], reason=b["reason"],
            reverse_checkin_ids=b.get("reverse_checkin_ids"),
            refunded_at=b["refunded_at"]).event_id}),
        ("POST", re.compile(r"^/bad-debts$"), lambda b: {"event_id": pf.bad_debt(
            bad_debt_id=b["bad_debt_id"], package_id=b["package_id"], reason=b["reason"],
            occurred_at=b["occurred_at"]).event_id}),
        ("POST", re.compile(r"^/transfers$"), lambda b: {"event_id": pf.transfer_package(
            transfer_id=b["transfer_id"], package_id=b["package_id"],
            to_member_id=b["to_member_id"], to_store_id=b.get("to_store_id"),
            sales_owner_id=b.get("sales_owner_id"), transferred_at=b["transferred_at"]).event_id}),
        ("POST", re.compile(r"^/settlements$"), settle),
        ("POST", re.compile(r"^/corrections$"), lambda b: {"event_id": pf.correct_period(
            correction_id=b["correction_id"], period=b["period"], reason=b["reason"],
            lines=b.get("lines"), late_checkins=b.get("late_checkins"),
            late_bookings=b.get("late_bookings"), created_at=b["created_at"]).event_id}),
        ("GET", re.compile(r"^/people/([^/]+)/statement$"),
         lambda m, _m: pf.coach_statement(m.group(1))),
        ("GET", re.compile(r"^/stores/comparison$"), lambda _m, _b: pf.store_comparison()),
        ("GET", re.compile(r"^/periods/([0-9]{4}-[0-9]{2})/batches$"),
         lambda m, _b: {"period": m.group(1), "batches": pf.period_batches(m.group(1))}),
        ("GET", re.compile(r"^/batches/([^/]+)/verify$"),
         lambda m, _b: pf.verify_batch(m.group(1))),
        ("GET", re.compile(r"^/batches/(.+)$"), lambda m, _b: _require_batch(pf, m.group(1))),
        ("GET", re.compile(r"^/events$"),
         lambda _m, _b: {"events": [e.to_dict() for e in pf.store.all()]}),
    ]


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._write_json(200, health_payload())
            return
        self._dispatch("GET", path, None)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (ValueError, UnicodeDecodeError) as exc:
            self._write_json(400, {"error": f"请求体解析失败: {exc}"})
            return
        self._dispatch("POST", path, body)

    def _dispatch(self, method: str, path: str, body: dict | None) -> None:
        pf = get_platform()
        for verb, pattern, handler in _routes(pf):
            if verb != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            try:
                if method == "GET":
                    result = handler(match, body)
                else:
                    result = handler(body)
            except DomainError as exc:
                self._write_json(422, {"error": str(exc)})
                return
            except EventConflictError as exc:
                self._write_json(409, {"error": str(exc)})
                return
            except (KeyError, TypeError, ValueError) as exc:
                self._write_json(400, {"error": f"参数错误: {exc}"})
                return
            except BatchNotFound:
                self._write_json(404, {"error": "批次不存在"})
                return
            self._write_json(200, result)
            return
        self._write_json(404, {"error": "Not Found"})

    def _write_json(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RequestHandler)
