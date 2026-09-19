"""月结锁定、更正批次与确定性重跑测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profitshare.engine import DomainError, SRC_CORRECTION
from profitshare.repository import EventStore
from profitshare.service import Platform

from support import make_platform, seed_class


class SettlementTest(unittest.TestCase):
    def test_settle_creates_stable_batch_number(self) -> None:
        pf = make_platform()
        seed_class(pf)
        pf.settle_month("2026-01")
        batches = pf.period_batches("2026-01")
        self.assertEqual(len(batches), 1)
        self.assertTrue(batches[0]["batch_no"].startswith("JS202601-"))
        self.assertEqual(batches[0]["state"], "locked")
        self.assertTrue(pf.verify_batch(batches[0]["batch_no"])["matches"])

    def test_locked_period_rejects_normal_writes(self) -> None:
        pf = make_platform()
        seed_class(pf)
        pf.settle_month("2026-01")
        # 锁定账期内的新预约/签到被拒
        pf.schedule_session(session_id="SES2", store_id="S1", coach_id="C-LI",
                            start_time="2026-01-22T19:00:00")
        with self.assertRaises(DomainError):
            pf.book_class(
                booking_id="BK2", session_id="SES2", member_id="M1",
                package_id="PKG1", booked_at="2026-01-21T10:00:00",
            )
        # 退款只能落在未锁定账期
        with self.assertRaises(DomainError):
            pf.refund(
                refund_id="RF1", package_id="PKG1", reason="1月内退",
                reverse_checkin_ids=["checkin:BK1"],
                refunded_at="2026-01-30T10:00:00",
            )

    def test_settle_is_idempotent_command(self) -> None:
        """重复月结命令幂等：返回同一批次，不产生第二个批次；
        事件流中若出现异源的第二次结算事件，引擎仍会拒绝。"""
        pf = make_platform()
        seed_class(pf)
        pf.settle_month("2026-01")
        pf.settle_month("2026-01")  # 命令重试，不报错
        self.assertEqual(len(pf.period_batches("2026-01")), 1)

        from profitshare.events import Event
        from profitshare.engine import materialize
        # 伪造一个不同 event_id 的第二次结算事件：引擎拒绝
        events = pf.store.all()
        rogue = Event(
            event_id="settle:rogue", event_type="month_settled",
            service_time="2026-01-31T23:59:59",
            payload={"period": "2026-01"}, recorded_at="2026-02-01T00:00:00",
        )
        with self.assertRaises(DomainError):
            materialize([*events, rogue])

    def test_correction_requires_reason(self) -> None:
        pf = make_platform()
        seed_class(pf)
        pf.settle_month("2026-01")
        with self.assertRaises(DomainError):
            pf.correct_period(
                correction_id="K1", period="2026-01", reason="   ",
                lines=[{"payee_type": "coach", "payee_id": "C-LI",
                        "store_id": "S1", "amount_cents": 100}],
                created_at="2026-02-05T10:00:00",
            )

    def test_manual_correction_appends_batch(self) -> None:
        pf = make_platform()
        seed_class(pf)
        pf.settle_month("2026-01")
        pf.correct_period(
            correction_id="K1", period="2026-01",
            reason="团课单价录错，少计提成",
            lines=[{"payee_type": "coach", "payee_id": "C-LI",
                    "store_id": "S1",
                    "amount_cents": 1200, "memo": "补提成差额"}],
            created_at="2026-02-05T10:00:00",
        )
        batches = pf.period_batches("2026-01")
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0]["state"], "corrected")
        self.assertTrue(batches[1]["batch_no"].endswith("-C1"))
        self.assertEqual(batches[1]["reason"], "团课单价录错，少计提成")
        self.assertEqual(batches[1]["totals_cents"], {"coach": {"CNY": 1200}})
        # 历史锁定批次内容不变
        self.assertEqual(
            pf.verify_batch(batches[0]["batch_no"])["matches"], True
        )
        # 教练台账能看到更正来源与理由
        li = pf.coach_statement("C-LI")
        correction_rows = [e for e in li["entries"] if e["source"] == SRC_CORRECTION]
        self.assertEqual(correction_rows[0]["amount_cents"], 1200)
        self.assertIn("少计提成", correction_rows[0]["memo"])
        self.assertEqual(li["net_cents"], 6000 + 1200)

    def test_late_checkin_via_correction_must_link_booking(self) -> None:
        """月结后补录签到：必须关联原预约，且只能走带理由的更正批次。"""
        pf = make_platform()
        seed_class(pf, booking_id="BK1", session_id="SES1")
        # 月结前已预约但未签到
        pf.schedule_session(session_id="SES2", store_id="S1", coach_id="C-LI",
                            start_time="2026-01-22T19:00:00")
        pf.book_class(
            booking_id="BK2", session_id="SES2", member_id="M1",
            package_id="PKG1", booked_at="2026-01-21T10:00:00",
        )
        pf.settle_month("2026-01")

        # 普通签到被拒
        with self.assertRaises(DomainError):
            pf.check_in(booking_id="BK2", recorded_at="2026-02-01T10:00:00")
        # 更正批次必须带 booking 关联
        with self.assertRaises(DomainError):
            pf.correct_period(
                correction_id="K0", period="2026-01", reason="补录",
                late_checkins=[{"unit_amount_cents": 20000}],
                created_at="2026-02-02T10:00:00",
            )
        # 正规补录
        pf.correct_period(
            correction_id="K1", period="2026-01",
            reason="会员出示纸质签到表，核对排课与预约后补录",
            late_checkins=[{"booking_id": "BK2", "unit_amount_cents": 20000}],
            created_at="2026-02-02T10:00:00",
        )
        batches = pf.period_batches("2026-01")
        c1 = batches[1]
        self.assertEqual(c1["totals_cents"],
                         {"store": {"CNY": 20000}, "coach": {"CNY": 6000}})
        # 不能对同一预约重复补录
        with self.assertRaises(DomainError):
            pf.correct_period(
                correction_id="K2", period="2026-01", reason="再补一次",
                late_checkins=[{"booking_id": "BK2", "unit_amount_cents": 20000}],
                created_at="2026-02-03T10:00:00",
            )

    def test_late_booking_backfill_via_correction(self) -> None:
        """连原预约都没录入的纸质单：补建预约 + 核销一起走更正批次。"""
        pf = make_platform()
        seed_class(pf)
        pf.schedule_session(session_id="SES3", store_id="S1", coach_id="C-WANG",
                            start_time="2026-01-25T19:00:00")
        pf.settle_month("2026-01")
        pf.correct_period(
            correction_id="K1", period="2026-01", reason="纸质预约单补录",
            late_bookings=[{
                "booking_id": "BK3", "session_id": "SES3", "member_id": "M1",
                "package_id": "PKG1", "qty": 1, "unit_amount_cents": 20000,
            }],
            created_at="2026-02-04T10:00:00",
        )
        self.assertEqual(pf.coach_statement("C-WANG")["net_cents"], 6000)
        self.assertEqual(pf.coach_statement("C-WANG")["entries"][0]["batch_no"],
                         pf.period_batches("2026-01")[1]["batch_no"])


class ReplayDeterminismTest(unittest.TestCase):
    def test_replay_produces_identical_batches(self) -> None:
        pf = make_platform()
        seed_class(pf, referral=True)
        pf.settle_month("2026-01")
        pf.refund(
            refund_id="RF1", package_id="PKG1", reason="会员退费",
            reverse_checkin_ids=["checkin:BK1"],
            refunded_at="2026-02-10T10:00:00",
        )
        pf.correct_period(
            correction_id="K1", period="2026-01", reason="提成基数补差",
            lines=[{"payee_type": "coach", "payee_id": "C-LI", "store_id": "S1",
                    "amount_cents": 300}],
            created_at="2026-02-12T10:00:00",
        )

        def snapshot() -> tuple:
            view = pf.rebuild_view()
            return (
                [
                    (e.entry_id, e.amount_cents, e.batch_no, e.source,
                     e.payee_type, e.payee_id, e.rule_version)
                    for e in view.entries
                ],
                [
                    (period, [b.batch_no for b in batches],
                     [b.state for b in batches])
                    for period, batches in sorted(view.settlements.items())
                ],
            )

        first = snapshot()
        for _ in range(3):
            self.assertEqual(snapshot(), first)

    def test_rebuild_from_json_file_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            pf = Platform(EventStore(path))
            pf.publish_rule(__import__(
                "profitshare.rules", fromlist=["RuleVersion"]
            ).RuleVersion("v1", "2026-01-01", coach_bps=3000))
            seed_class(pf)
            pf.settle_month("2026-01")
            batch_no = pf.period_batches("2026-01")[0]["batch_no"]

            # 全新进程视角：从 JSON 重建，批次号必须一致
            pf2 = Platform(EventStore(path))
            self.assertEqual(
                pf2.period_batches("2026-01")[0]["batch_no"], batch_no
            )
            self.assertTrue(
                pf2.verify_batch(batch_no)["matches"]
            )

    def test_idempotent_event_retry(self) -> None:
        """同一 event_id 重放写入不会产生重复数据。"""
        pf = make_platform()
        seed_class(pf)
        n = len(pf.store)
        # 用完全相同的参数再发一次购课事件（同一确定性 event_id）
        pf.purchase_package(
            package_id="PKG1", member_id="M1", store_id="S1", total_qty=10,
            total_amount_cents=200000, sales_owner_id="SALES-A",
            sold_at="2026-01-02T10:00:00",
        )
        self.assertEqual(len(pf.store), n)


if __name__ == "__main__":
    unittest.main()
