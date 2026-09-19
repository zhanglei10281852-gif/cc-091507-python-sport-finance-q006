from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain import ConflictError, NotFoundError
from engine import compute_entries
from ledger import Ledger
from reports import Reports
from projection import Service


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.ledger = Ledger(self.dir)
        self.svc = Service(self.ledger)
        self.rpt = Reports(self.ledger)

    def seed(self) -> None:
        self.svc.publish_rule("2026-08-01", "0.4", "0.1", version_id="v1")
        self.svc.register_store("一店", "st1")
        self.svc.register_person("教练甲", "coach", "c1", "st1")
        self.svc.register_person("销售甲", "sales", "s1", "st1")
        self.svc.sell_package({
            "sale_id": "sale1", "package_id": "PKG", "member_id": "m1",
            "sessions": 10, "price_cents": 100000, "store_id": "st1",
            "salesperson_id": "s1", "referred": True, "sale_date": "2026-08-02",
        })
        self.svc.create_session({
            "session_id": "cls1", "class_type": "团课", "store_id": "st1",
            "scheduled_date": "2026-08-10", "coach_id": "c1",
        })
        self.svc.create_booking({
            "booking_id": "bkg1", "session_id": "cls1",
            "member_id": "m1", "sale_id": "sale1",
        })
        self.svc.check_in({"booking_id": "bkg1"})


class CommissionTest(LedgerCase):
    def test_checkin_confirms_commission_and_revenue(self) -> None:
        self.seed()
        # 每次核销 10000 分；提成 40% = 4000，门店留存 6000。
        earn = self.rpt.coach_earnings("c1", "2026-08")
        self.assertEqual(4000, earn["total_cents"])
        sales = self.rpt.coach_earnings("s1", "2026-08")
        self.assertEqual(10000, sales["total_cents"])  # 拉新 10%
        cmp = self.rpt.store_comparison("2026-08")
        row = next(r for r in cmp["stores"] if r["store_id"] == "st1")
        self.assertEqual(6000, row["revenue_cents"])
        self.assertEqual(1, row["sessions_served"])
        self.assertAlmostEqual(0.6, row["retention_rate"])

    def test_referral_only_on_first_sale(self) -> None:
        self.seed()
        with self.assertRaises(ConflictError):
            self.svc.sell_package({
                "sale_id": "sale2", "package_id": "PKG2", "member_id": "m1",
                "sessions": 1, "price_cents": 1000, "store_id": "st1",
                "salesperson_id": "s1", "referred": True, "sale_date": "2026-08-03",
            })

    def test_no_double_counting_commission_vs_referral(self) -> None:
        """同一节课只产生一次授课提成；拉新奖励只在成交时一次。"""
        self.seed()
        entries = [e for e in compute_entries(self.ledger.events)
                   if e["settleable"] and e["period"] == "2026-08"]
        commissions = [e for e in entries if e["kind"] == "coach_commission"]
        referrals = [e for e in entries if e["kind"] == "sales_referral"]
        self.assertEqual(1, len(commissions))
        self.assertEqual(1, len(referrals))
        # 个人应得合计 = 提成 4000 + 拉新 10000，与门店留存 6000 互不重叠。
        person_total = sum(
            e["amount_cents"] for e in entries if e["person_id"] is not None
        )
        store_total = sum(
            e["amount_cents"] for e in entries if e["kind"] == "store_revenue"
        )
        self.assertEqual(14000, person_total)
        self.assertEqual(6000, store_total)


class SubstitutionTest(LedgerCase):
    def test_substitute_only_affects_future_service(self) -> None:
        self.seed()
        self.svc.register_person("教练乙", "coach", "c2", "st1")
        self.svc.create_session({
            "session_id": "cls2", "class_type": "团课", "store_id": "st1",
            "scheduled_date": "2026-08-20", "coach_id": "c1",
        })
        self.svc.substitute_coach("cls2", "c2", "教练甲请假")
        self.svc.create_booking({
            "booking_id": "bkg2", "session_id": "cls2",
            "member_id": "m1", "sale_id": "sale1",
        })
        self.svc.check_in({"booking_id": "bkg2"})
        self.assertEqual(4000, self.rpt.coach_earnings("c1", "2026-08")["total_cents"])
        self.assertEqual(4000, self.rpt.coach_earnings("c2", "2026-08")["total_cents"])


class ReversalTest(LedgerCase):
    def test_reversal_links_original_and_frees_slot(self) -> None:
        self.seed()
        # 找到刚写入的签到
        checkin = next(
            e.payload for e in self.ledger.events
            if e.type == "check_in_recorded" and e.payload["booking_id"] == "bkg1"
        )
        # 冲销必须给原因
        with self.assertRaises(Exception):
            self.svc.reverse_checkin(checkin["checkin_id"], "")
        self.svc.reverse_checkin(checkin["checkin_id"], "会员当天未实际到店")
        earn = self.rpt.coach_earnings("c1", "2026-08")
        self.assertEqual(0, earn["total_cents"])
        self.assertEqual(4000, earn["gross_cents"])
        self.assertEqual(4000, earn["deductions_cents"])
        # 反向分录必须链接原分录
        entries = compute_entries(self.ledger.events)
        reversal = next(e for e in entries if e["source"] == "checkin_reversal")
        original = next(e for e in entries
                        if e["kind"] == "coach_commission" and e["amount_cents"] > 0)
        self.assertEqual(original["entry_id"], reversal["linked_entry_id"])
        # 历史原分录仍然存在
        self.assertIsNotNone(original)
        # 槽位释放：可再次核销，且净效果只有一次提成
        self.svc.check_in({"booking_id": "bkg1"})
        self.assertEqual(4000, self.rpt.coach_earnings("c1", "2026-08")["total_cents"])

    def test_checkin_requires_booking_link(self) -> None:
        self.seed()
        with self.assertRaises(NotFoundError):
            self.svc.check_in({"booking_id": "not-exist"})

    def test_duplicate_checkin_rejected(self) -> None:
        self.seed()
        with self.assertRaises(ConflictError):
            self.svc.check_in({"booking_id": "bkg1"})


class TransferTest(LedgerCase):
    def test_revenue_stays_where_service_actually_happened(self) -> None:
        self.seed()  # 8/10 在 st1 核销一次
        self.svc.register_store("二店", "st2")
        self.svc.register_person("教练丙", "coach", "c3", "st2")
        self.svc.transfer_package("sale1", "m9", "st2")
        self.svc.create_session({
            "session_id": "cls9", "class_type": "团课", "store_id": "st2",
            "scheduled_date": "2026-08-22", "coach_id": "c3",
        })
        # 课包已属于 m9，旧会员不能再用
        with self.assertRaises(ConflictError):
            self.svc.create_booking({
                "booking_id": "bX", "session_id": "cls9",
                "member_id": "m1", "sale_id": "sale1",
            })
        self.svc.create_booking({
            "booking_id": "bkg9", "session_id": "cls9",
            "member_id": "m9", "sale_id": "sale1",
        })
        self.svc.check_in({"booking_id": "bkg9"})
        cmp = self.rpt.store_comparison("2026-08")
        revenue = {r["store_id"]: r["revenue_cents"] for r in cmp["stores"]}
        self.assertEqual(6000, revenue["st1"])  # 转让前的服务留在 st1
        self.assertEqual(6000, revenue["st2"])  # 转让后的服务记到 st2


class RefundBadDebtTest(LedgerCase):
    def test_refund_creates_reversing_entries(self) -> None:
        self.seed()
        self.svc.refund({
            "sale_id": "sale1", "amount_cents": 10000,
            "reason": "会员退卡", "date": "2026-09-05",
        })
        # 9 月反向：教练 -4000，门店 -6000；原 8 月分录保留
        c1 = self.rpt.coach_earnings("c1", "2026-09")
        self.assertEqual(-4000, c1["total_cents"])
        c1_aug = self.rpt.coach_earnings("c1", "2026-08")
        self.assertEqual(4000, c1_aug["total_cents"])
        # 拉新按比例追回 1000
        s1_sep = self.rpt.coach_earnings("s1", "2026-09")
        self.assertEqual(-1000, s1_sep["total_cents"])

    def test_refund_cannot_exceed_price(self) -> None:
        self.seed()
        with self.assertRaises(ConflictError):
            self.svc.refund({
                "sale_id": "sale1", "amount_cents": 100001,
                "reason": "x", "date": "2026-09-05",
            })

    def test_bad_debt_only_for_credit_sales(self) -> None:
        self.seed()
        with self.assertRaises(ConflictError):
            self.svc.bad_debt({
                "sale_id": "sale1", "amount_cents": 100,
                "reason": "失联", "date": "2026-09-05",
            })

    def test_refund_requires_reason(self) -> None:
        self.seed()
        with self.assertRaises(Exception):
            self.svc.refund({
                "sale_id": "sale1", "amount_cents": 100, "date": "2026-09-05",
            })


class SettlementTest(LedgerCase):
    def test_lock_snapshot_is_immutable_and_deterministic(self) -> None:
        self.seed()
        lock = self.svc.lock_settlement("2026-08")
        self.assertEqual("STL-2026-08-CNY", lock["batch_id"])
        first = self.rpt.settlement_batch("2026-08")
        persons = {p["person_id"]: p["net_cents"] for p in first["people"]}
        self.assertEqual(4000, persons["c1"])
        self.assertEqual(10000, persons["s1"])
        snapshot_count = first["entry_count"]

        # 锁定后再来的 8 月数据，不影响锁定快照
        self.svc.register_person("教练乙", "coach", "c2", "st1")
        self.svc.create_session({
            "session_id": "cls2", "store_id": "st1", "class_type": "团课",
            "scheduled_date": "2026-08-28", "coach_id": "c2",
        })
        self.svc.create_booking({
            "booking_id": "bkg2", "session_id": "cls2",
            "member_id": "m1", "sale_id": "sale1",
        })
        with self.assertRaises(ConflictError):
            self.svc.check_in({"booking_id": "bkg2", "source": "backfill"})

        # 必须开带理由的更正批次才能追加
        with self.assertRaises(Exception):
            self.svc.open_correction("2026-08", "  ")
        cor = self.svc.open_correction("2026-08", "8月28日漏签补录")
        self.assertEqual("COR-2026-08-CNY-01", cor["correction_batch_id"])
        self.svc.check_in({
            "booking_id": "bkg2", "source": "backfill",
            "reason": "纸质签到表补录",
            "correction_batch_id": cor["correction_batch_id"],
        })
        self.svc.post_correction(cor["correction_batch_id"], [{
            "target_type": "person", "target_id": "s1",
            "amount_cents": -500, "reason": "拉新奖励多算",
        }])

        again = self.rpt.settlement_batch("2026-08")
        self.assertEqual(snapshot_count, again["entry_count"])  # 原快照不变
        cb = self.rpt.correction_batch("COR-2026-08-CNY-01")
        self.assertEqual("posted", cb["status"])
        self.assertEqual(-500, next(
            p["net_cents"] for p in cb["people"] if p["person_id"] == "s1"))
        # 已过账批次不能再追加
        with self.assertRaises(ConflictError):
            self.svc.post_correction("COR-2026-08-CNY-01")

        # 重跑：编号与金额完全稳定
        ledger2 = Ledger(self.dir)
        rpt2 = Reports(ledger2)
        re_run = rpt2.settlement_batch("2026-08")
        self.assertEqual(first["batch_id"], re_run["batch_id"])
        self.assertEqual(
            [(e["entry_id"], e["amount_cents"]) for e in first["entries"]],
            [(e["entry_id"], e["amount_cents"]) for e in re_run["entries"]],
        )
        cb2 = rpt2.correction_batch("COR-2026-08-CNY-01")
        self.assertEqual(
            [(e["entry_id"], e["amount_cents"]) for e in cb["entries"]],
            [(e["entry_id"], e["amount_cents"]) for e in cb2["entries"]],
        )

    def test_cannot_lock_twice(self) -> None:
        self.seed()
        self.svc.lock_settlement("2026-08")
        with self.assertRaises(ConflictError):
            self.svc.lock_settlement("2026-08")


class RuleVersionTest(LedgerCase):
    def test_rule_chosen_by_service_date(self) -> None:
        self.svc.register_store("店", "st1")
        self.svc.register_person("教练", "coach", "c1", "st1")
        self.svc.publish_rule("2026-08-01", "0.4", "0.0", version_id="old")
        self.svc.publish_rule("2026-09-01", "0.5", "0.0", version_id="new")
        # 8 月卖的课包
        self.svc.sell_package({
            "sale_id": "s1", "package_id": "P", "member_id": "m1",
            "sessions": 2, "price_cents": 20000, "store_id": "st1",
            "sale_date": "2026-08-15",
        })
        self.svc.create_session({
            "session_id": "k1", "store_id": "st1", "class_type": "课",
            "scheduled_date": "2026-08-30", "coach_id": "c1",
        })
        self.svc.create_session({
            "session_id": "k2", "store_id": "st1", "class_type": "课",
            "scheduled_date": "2026-09-02", "coach_id": "c1",
        })
        self.svc.create_booking({"booking_id": "b1", "session_id": "k1",
                                 "member_id": "m1", "sale_id": "s1"})
        self.svc.create_booking({"booking_id": "b2", "session_id": "k2",
                                 "member_id": "m1", "sale_id": "s1"})
        self.svc.check_in({"booking_id": "b1"})
        self.svc.check_in({"booking_id": "b2"})
        self.assertEqual(4000, self.rpt.coach_earnings("c1", "2026-08")["total_cents"])
        self.assertEqual(5000, self.rpt.coach_earnings("c1", "2026-09")["total_cents"])


if __name__ == "__main__":
    unittest.main()
