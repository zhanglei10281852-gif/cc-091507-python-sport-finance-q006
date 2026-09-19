"""HTTP 接口端到端测试（标准库 http.client 直连本地线程服务器）。"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
import urllib.error
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import app


class HttpTest(unittest.TestCase):
    server = None
    thread = None
    port = 0

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = app.create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        app.reset_platform(None)  # 每个用例全新内存库

    def _call(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json; charset=utf-8"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        data = json.loads(raw) if raw else {}
        conn.close()
        return resp.status, data

    def test_health(self) -> None:
        status, data = self._call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_full_flow_over_http(self) -> None:
        status, _ = self._call("POST", "/rules", {
            "version_id": "v2026-01", "effective_from": "2026-01-01",
            "coach_bps": 3000, "referral_reward_cents": 5000,
        })
        self.assertEqual(status, 200)

        self.assertEqual(200, self._call("POST", "/packages", {
            "package_id": "PKG1", "member_id": "M1", "store_id": "S1",
            "total_qty": 10, "total_amount_cents": 200000,
            "sales_owner_id": "SALES-A", "sold_at": "2026-01-02T10:00:00",
        })[0])
        self.assertEqual(200, self._call("POST", "/referrals", {
            "referral_id": "R1", "referred_member_id": "M1",
            "sales_owner_id": "SALES-A", "store_id": "S1",
            "occurred_at": "2026-01-02T09:00:00",
        })[0])
        self.assertEqual(200, self._call("POST", "/sessions", {
            "session_id": "SES1", "store_id": "S1", "coach_id": "C-LI",
            "start_time": "2026-01-10T19:00:00",
        })[0])
        self.assertEqual(200, self._call("POST", "/bookings", {
            "booking_id": "BK1", "session_id": "SES1", "member_id": "M1",
            "package_id": "PKG1", "booked_at": "2026-01-09T10:00:00",
        })[0])
        self.assertEqual(200, self._call("POST", "/substitutes", {
            "session_id": "SES1", "original_coach_id": "C-LI",
            "substitute_coach_id": "C-WANG", "reason": "发烧",
            "registered_at": "2026-01-10T15:00:00",
        })[0])
        self.assertEqual(200, self._call("POST", "/checkins", {
            "booking_id": "BK1", "unit_amount_cents": 20000,
            "recorded_at": "2026-01-10T20:00:00",
        })[0])

        # 教练台账
        status, wang = self._call("GET", "/people/C-WANG/statement")
        self.assertEqual(status, 200)
        self.assertEqual(wang["net_cents"], 6000)
        self.assertEqual(wang["entries"][0]["source"], "授课提成")

        # 月结前先排好 1 月 20 日的课并完成预约（当时忘了签到）
        self.assertEqual(200, self._call("POST", "/sessions", {
            "session_id": "SES2", "store_id": "S1", "coach_id": "C-LI",
            "start_time": "2026-01-20T19:00:00",
        })[0])
        self.assertEqual(200, self._call("POST", "/bookings", {
            "booking_id": "BK2", "session_id": "SES2", "member_id": "M1",
            "package_id": "PKG1", "booked_at": "2026-01-19T10:00:00",
        })[0])

        # 月结
        status, settle = self._call("POST", "/settlements", {"period": "2026-01"})
        self.assertEqual(status, 200)
        batch_no = settle["batch_no"]

        # 锁定后普通签到 422
        status, err = self._call("POST", "/checkins", {
            "booking_id": "BK2", "recorded_at": "2026-02-01T10:00:00",
        })
        self.assertEqual(status, 422)
        self.assertIn("锁定", err["error"])

        # 带理由的更正批次补录
        status, corr = self._call("POST", "/corrections", {
            "correction_id": "K1", "period": "2026-01",
            "reason": "纸质签到表核对一致",
            "late_checkins": [{"booking_id": "BK2", "unit_amount_cents": 20000}],
            "created_at": "2026-02-03T10:00:00",
        })
        self.assertEqual(status, 200)

        # 批次核验
        status, verify = self._call("GET", f"/batches/{batch_no}/verify")
        self.assertEqual(status, 200)
        self.assertTrue(verify["matches"])

        # 门店对比
        status, comparison = self._call("GET", "/stores/comparison")
        self.assertEqual(status, 200)
        self.assertTrue(comparison["stores"])

        # 不存在的批次 404
        self.assertEqual(404, self._call("GET", "/batches/NOPE")[0])

    def test_validation_error_is_422(self) -> None:
        self.assertEqual(200, self._call("POST", "/rules", {
            "version_id": "v1", "effective_from": "2026-01-01",
        })[0])
        # 没有课包就预约 -> 422 业务错误
        status, data = self._call("POST", "/bookings", {
            "booking_id": "BKX", "session_id": "NOPE", "member_id": "M1",
            "package_id": "NOPE", "booked_at": "2026-01-09T10:00:00",
        })
        self.assertEqual(status, 422)
        self.assertTrue(data["error"])


if __name__ == "__main__":
    unittest.main()
