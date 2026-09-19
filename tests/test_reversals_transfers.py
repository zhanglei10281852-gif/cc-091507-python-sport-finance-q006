"""退款、坏账、课包转让测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profitshare.engine import (
    SRC_BAD_DEBT_REVERSAL,
    SRC_REFUND_REVERSAL,
    DomainError,
)

from support import make_platform, seed_class


class RefundTest(unittest.TestCase):
    def test_refund_creates_reversals_in_current_period(self) -> None:
        pf = make_platform()
        seed_class(pf, referral=True)
        pf.settle_month("2026-01")
        pf.refund(
            refund_id="RF1", package_id="PKG1", reason="会员受伤退费",
            reverse_checkin_ids=["checkin:BK1"],
            refunded_at="2026-02-10T10:00:00",
        )
        view = pf.rebuild_view()
        feb = sorted(
            (e for e in view.entries if e.period == "2026-02"),
            key=lambda e: e.entry_id,
        )
        self.assertEqual(len(feb), 3)  # 收入 + 提成 + 拉新全冲回
        self.assertTrue(all(e.reversal_of for e in feb))
        self.assertTrue(all(e.source == SRC_REFUND_REVERSAL for e in feb))
        self.assertTrue(all(e.amount_cents < 0 for e in feb))
        # 原 1 月锁定分录原样保留
        locked = view.settlements["2026-01"][0]
        original = view.entries_of_batch(locked.batch_no)
        self.assertTrue(all(e.amount_cents > 0 for e in original))
        # 教练台账同时显示收入与扣减
        coach = pf.coach_statement("C-LI")
        self.assertTrue(any(e["amount_cents"] < 0 for e in coach["entries"]))
        self.assertEqual(coach["earned_cents"], 6000)
        self.assertEqual(coach["deductions_cents"], -6000)

    def test_single_session_refund_keeps_referral_reward(self) -> None:
        """会员还有其他有效核销时，单次课退款不追回拉新奖励。"""
        pf = make_platform()
        # 第一次核销触发拉新
        seed_class(pf, referral=True, booking_id="BK1", session_id="SES1")
        # 同课包/同会员第二次核销
        pf.schedule_session(session_id="SES2", store_id="S1", coach_id="C-LI",
                            start_time="2026-01-17T19:00:00")
        pf.book_class(booking_id="BK2", session_id="SES2", member_id="M1",
                      package_id="PKG1", booked_at="2026-01-16T10:00:00")
        pf.check_in(booking_id="BK2", unit_amount_cents=20000,
                    recorded_at="2026-01-17T20:00:00")
        pf.refund(
            refund_id="RF1", package_id="PKG1", reason="单次课争议退费",
            reverse_checkin_ids=["checkin:BK1"],
            refunded_at="2026-01-20T10:00:00",
        )
        st = pf.coach_statement("SALES-A")
        self.assertEqual(st["net_cents"], 5000)  # 拉新保留
        coach = pf.coach_statement("C-LI")
        # 收入20000*0.3*2=12000；冲回一次6000，净得 6000
        self.assertEqual(coach["deductions_cents"], -6000)

    def test_refund_requires_reason(self) -> None:
        pf = make_platform()
        seed_class(pf)
        with self.assertRaises(DomainError):
            pf.refund(
                refund_id="RF1", package_id="PKG1", reason="",
                reverse_checkin_ids=["checkin:BK1"],
                refunded_at="2026-01-20T10:00:00",
            )

    def test_duplicate_reversal_rejected(self) -> None:
        pf = make_platform()
        seed_class(pf, referral=True)
        pf.refund(
            refund_id="RF1", package_id="PKG1", reason="退",
            reverse_checkin_ids=["checkin:BK1"],
            refunded_at="2026-01-20T10:00:00",
        )
        with self.assertRaises(DomainError):
            pf.refund(
                refund_id="RF2", package_id="PKG1", reason="再退一次",
                reverse_checkin_ids=["checkin:BK1"],
                refunded_at="2026-01-21T10:00:00",
            )

    def test_refund_wrong_package_rejected(self) -> None:
        pf = make_platform()
        seed_class(pf)
        pf.purchase_package(
            package_id="PKG2", member_id="M9", store_id="S1", total_qty=4,
            total_amount_cents=80000, sold_at="2026-01-05T10:00:00",
        )
        with self.assertRaises(DomainError):
            pf.refund(
                refund_id="RF1", package_id="PKG2", reason="张冠李戴",
                reverse_checkin_ids=["checkin:BK1"],
                refunded_at="2026-01-20T10:00:00",
            )

    def test_unconsumed_refund_no_entries(self) -> None:
        pf = make_platform()
        seed_class(pf)
        before = len(pf.rebuild_view().entries)
        # 课包内未核销部分的退款：不产生损益分录
        pf.refund(
            refund_id="RF0", package_id="PKG1", reason="剩余课节全退",
            refunded_at="2026-01-20T10:00:00",
        )
        self.assertEqual(len(pf.rebuild_view().entries), before)


class BadDebtTest(unittest.TestCase):
    def test_bad_debt_reverses_chain(self) -> None:
        pf = make_platform()
        seed_class(pf, referral=True)
        pf.bad_debt(
            bad_debt_id="BD1", package_id="PKG1", reason="会员失联欠费",
            occurred_at="2026-01-25T10:00:00",
        )
        view = pf.rebuild_view()
        reversals = [
            e for e in view.entries if e.source == SRC_BAD_DEBT_REVERSAL
        ]
        self.assertEqual(len(reversals), 3)
        self.assertTrue(all(e.amount_cents < 0 for e in reversals))
        self.assertEqual(pf.coach_statement("C-LI")["net_cents"], 0)
        self.assertEqual(pf.coach_statement("SALES-A")["net_cents"], 0)

    def test_bad_debt_then_booking_rejected(self) -> None:
        pf = make_platform()
        seed_class(pf)
        pf.bad_debt(
            bad_debt_id="BD1", package_id="PKG1", reason="失联",
            occurred_at="2026-01-25T10:00:00",
        )
        pf.schedule_session(session_id="SES9", store_id="S1", coach_id="C-LI",
                            start_time="2026-01-28T19:00:00")
        with self.assertRaises(DomainError):
            pf.book_class(
                booking_id="BK9", session_id="SES9", member_id="M1",
                package_id="PKG1", booked_at="2026-01-27T10:00:00",
            )

    def test_duplicate_bad_debt_rejected(self) -> None:
        pf = make_platform()
        seed_class(pf)
        pf.bad_debt(
            bad_debt_id="BD1", package_id="PKG1", reason="失联",
            occurred_at="2026-01-25T10:00:00",
        )
        with self.assertRaises(DomainError):
            pf.bad_debt(
                bad_debt_id="BD2", package_id="PKG1", reason="又记一次",
                occurred_at="2026-01-26T10:00:00",
            )


class TransferTest(unittest.TestCase):
    def test_transfer_keeps_historical_revenue_at_original_store(self) -> None:
        pf = make_platform()
        seed_class(pf, store_id="S1")
        pf.transfer_package(
            transfer_id="T1", package_id="PKG1", to_member_id="M2",
            to_store_id="S2", transferred_at="2026-01-15T10:00:00",
        )
        # 受让会员在 S2 上剩余课节
        pf.schedule_session(session_id="SES2", store_id="S2", coach_id="C-Z",
                            start_time="2026-01-20T19:00:00")
        pf.book_class(booking_id="BK2", session_id="SES2", member_id="M2",
                      package_id="PKG1", booked_at="2026-01-19T10:00:00")
        pf.check_in(booking_id="BK2", unit_amount_cents=20000,
                    recorded_at="2026-01-20T20:00:00")
        view = pf.rebuild_view()
        s1 = sum(
            e.amount_cents for e in view.entries
            if e.store_id == "S1" and e.payee_type == "store"
        )
        s2 = sum(
            e.amount_cents for e in view.entries
            if e.store_id == "S2" and e.payee_type == "store"
        )
        self.assertEqual(s1, 20000)  # 历史收入留在 S1
        self.assertEqual(s2, 20000)  # 转让后核销归 S2
        # 受让不产生新的拉新奖励
        self.assertEqual(pf.coach_statement("SALES-A")["net_cents"], 0)

    def test_transfer_to_same_member_rejected(self) -> None:
        pf = make_platform()
        seed_class(pf)
        with self.assertRaises(DomainError):
            pf.transfer_package(
                transfer_id="T1", package_id="PKG1", to_member_id="M1",
                transferred_at="2026-01-15T10:00:00",
            )


if __name__ == "__main__":
    unittest.main()
