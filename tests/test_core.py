"""核心分润行为测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profitshare.engine import (
    SRC_COACH_COMMISSION,
    SRC_REVENUE,
    DomainError,
)
from profitshare.money import split_cents, yuan_to_cents
from profitshare.rules import RuleVersion
from profitshare.service import Platform

from support import make_platform, make_two_rule_platform, seed_class


class MoneyTest(unittest.TestCase):
    def test_yuan_to_cents(self) -> None:
        self.assertEqual(yuan_to_cents("199.99"), 19999)
        self.assertEqual(yuan_to_cents("0.005"), 1)  # 四舍五入
        self.assertEqual(yuan_to_cents(200), 20000)

    def test_split_cents_sums_exactly(self) -> None:
        for total in (100, 1, 7, 10003, -100, -7):
            shares = split_cents(total, [30, 30, 40])
            self.assertEqual(sum(shares), total, total)
            self.assertTrue(all(s <= 0 for s in shares) if total < 0 else True)

    def test_split_zero_weights(self) -> None:
        shares = split_cents(100, [0, 50, 50])
        self.assertEqual(shares[0], 0)
        self.assertEqual(sum(shares), 100)


class CommissionTest(unittest.TestCase):
    def test_checkin_generates_revenue_and_commission(self) -> None:
        pf = make_platform()
        seed_class(pf)
        view = pf.rebuild_view()
        by_source = {}
        for e in view.entries:
            by_source.setdefault(e.source, []).append(e)
        self.assertEqual(
            [e.amount_cents for e in by_source[SRC_REVENUE]], [20000]
        )
        self.assertEqual(
            [e.amount_cents for e in by_source[SRC_COACH_COMMISSION]], [6000]
        )
        # 分录快照规则版本
        for entries in by_source.values():
            for e in entries:
                self.assertEqual(e.rule_version, "v2026-01")

    def test_no_rule_version_raises(self) -> None:
        pf = Platform()  # 未发布规则
        with self.assertRaises(DomainError):
            seed_class(pf)

    def test_substitute_gets_commission(self) -> None:
        """教练请假替补：提成发给实际授课的替补教练。"""
        pf = make_platform()
        pf.purchase_package(
            package_id="PKG1", member_id="M1", store_id="S1", total_qty=10,
            total_amount_cents=200000, sold_at="2026-01-02T10:00:00",
        )
        pf.schedule_session(
            session_id="SES1", store_id="S1", coach_id="C-LI",
            start_time="2026-01-10T19:00:00",
        )
        pf.book_class(
            booking_id="BK1", session_id="SES1", member_id="M1",
            package_id="PKG1", booked_at="2026-01-09T10:00:00",
        )
        pf.register_substitute(
            session_id="SES1", original_coach_id="C-LI",
            substitute_coach_id="C-WANG", reason="李教练发烧请假",
            registered_at="2026-01-10T15:00:00",
        )
        pf.check_in(booking_id="BK1", unit_amount_cents=20000,
                    recorded_at="2026-01-10T20:00:00")
        self.assertEqual(pf.coach_statement("C-WANG")["net_cents"], 6000)
        self.assertEqual(pf.coach_statement("C-LI")["net_cents"], 0)
        # 替补原因与关联记录写在台账里
        row = pf.coach_statement("C-WANG")["entries"][0]
        self.assertIn("替补", row["memo"])
        self.assertEqual(row["linked_record_id"], "BK1")

    def test_substitute_requires_reason_and_before_class(self) -> None:
        pf = make_platform()
        seed_class(pf, booking_id="BKX", session_id="SESX")
        with self.assertRaises(DomainError):
            pf.register_substitute(
                session_id="SESX", original_coach_id="C-LI",
                substitute_coach_id="C-WANG", reason="",
                registered_at="2026-01-10T18:00:00",
            )
        with self.assertRaises(DomainError):
            pf.register_substitute(
                session_id="SESX", original_coach_id="C-LI",
                substitute_coach_id="C-Z", reason="迟到",
                registered_at="2026-01-11T18:00:00",  # 开课后
            )


class RuleVersionTest(unittest.TestCase):
    def test_commission_uses_rule_active_at_service_time(self) -> None:
        pf = make_two_rule_platform()
        # 1 月课按 30%，2 月课按 40%
        seed_class(pf, class_time="2026-01-15T19:00:00", booking_id="BK1",
                   session_id="SES1", package_id="PKG1", unit_price=10000)
        seed_class(pf, class_time="2026-02-05T19:00:00", booking_id="BK2",
                   session_id="SES2", package_id="PKG2", unit_price=10000,
                   sold_at="2026-02-01T10:00:00")
        li = pf.coach_statement("C-LI")
        self.assertEqual(li["breakdown_cents"]["commission"], 3000 + 4000)
        versions = {e["rule_version"] for e in li["entries"]}
        self.assertEqual(versions, {"v2026-01", "v2026-02"})

    def test_late_recorded_checkin_still_uses_service_time_rule(self) -> None:
        """补录：接收时间在 3 月，但服务发生在 1 月，仍按 1 月规则。"""
        pf = make_two_rule_platform()
        pf.purchase_package(
            package_id="PKG1", member_id="M1", store_id="S1", total_qty=10,
            total_amount_cents=200000, sold_at="2026-01-02T10:00:00",
        )
        pf.schedule_session(
            session_id="SES1", store_id="S1", coach_id="C-LI",
            start_time="2026-01-20T19:00:00",
        )
        pf.book_class(
            booking_id="BK1", session_id="SES1", member_id="M1",
            package_id="PKG1", booked_at="2026-01-19T10:00:00",
        )
        pf.check_in(
            booking_id="BK1", unit_amount_cents=10000,
            service_time="2026-01-20T19:00:00",
            recorded_at="2026-03-02T09:00:00",  # 3 月才补录进系统
        )
        entry = [
            e for e in pf.rebuild_view().entries
            if e.source == SRC_COACH_COMMISSION
        ][0]
        self.assertEqual(entry.rule_version, "v2026-01")
        self.assertEqual(entry.period, "2026-01")
        self.assertEqual(entry.amount_cents, 3000)


class ReferralTest(unittest.TestCase):
    def test_referral_reward_paid_once_on_first_checkin(self) -> None:
        pf = make_platform()
        seed_class(pf, referral=True, booking_id="BK1", session_id="SES1")
        # 同会员第二次核销不再发拉新
        pf.schedule_session(session_id="SES2", store_id="S1", coach_id="C-LI",
                            start_time="2026-01-17T19:00:00")
        pf.book_class(booking_id="BK2", session_id="SES2", member_id="M1",
                      package_id="PKG1", booked_at="2026-01-16T10:00:00")
        pf.check_in(booking_id="BK2", unit_amount_cents=20000,
                    recorded_at="2026-01-17T20:00:00")
        st = pf.coach_statement("SALES-A")
        self.assertEqual(st["breakdown_cents"]["referral"], 5000)
        self.assertEqual(len(st["entries"]), 1)

    def test_duplicate_referral_rejected(self) -> None:
        pf = make_platform()
        seed_class(pf, referral=True)
        with self.assertRaises(DomainError):
            pf.register_referral(
                referral_id="REF-OTHER", referred_member_id="M1",
                sales_owner_id="SALES-B", store_id="S1",
                occurred_at="2026-01-03T10:00:00",
            )


class CheckinGuardTest(unittest.TestCase):
    def test_checkin_requires_booking_link(self) -> None:
        pf = make_platform()
        with self.assertRaises(DomainError):
            pf.check_in(booking_id="NO-SUCH-BOOKING",
                        unit_amount_cents=10000,
                        recorded_at="2026-01-10T20:00:00")

    def test_double_checkin_rejected(self) -> None:
        pf = make_platform()
        seed_class(pf)
        # 相同命令的网络重试（同一 event_id、相同请求体）幂等，不报错也不重复入账
        pf.check_in(booking_id="BK1", unit_amount_cents=20000,
                    recorded_at="2026-01-10T19:00:00")
        entries_after_retry = len(pf.rebuild_view().entries)
        # 另一笔针对同一预约的新签到命令（新 event_id）必须被拒绝
        with self.assertRaises(DomainError):
            pf.check_in(booking_id="BK1", unit_amount_cents=20000,
                        recorded_at="2026-01-11T20:00:00",
                        event_id="checkin:BK1:dup")
        self.assertEqual(len(pf.rebuild_view().entries), entries_after_retry)


if __name__ == "__main__":
    unittest.main()
