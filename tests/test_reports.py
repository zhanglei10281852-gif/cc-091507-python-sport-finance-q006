"""门店报表与人效测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from support import make_platform, seed_class


class StoreReportTest(unittest.TestCase):
    def test_store_comparison_revenue_retention_efficiency(self) -> None:
        pf = make_platform()
        # S1：一次核销 20000，提成 6000
        seed_class(pf, store_id="S1", coach_id="C-LI", member_id="M1",
                   package_id="PKG1", session_id="SES1", booking_id="BK1",
                   sales_owner_id=None)
        # S2：两次核销共 40000，提成 12000，由同一教练完成
        seed_class(pf, store_id="S2", coach_id="C-Z", member_id="M2",
                   package_id="PKG2", session_id="SES2", booking_id="BK2",
                   sales_owner_id=None)
        pf.schedule_session(session_id="SES3", store_id="S2", coach_id="C-Z",
                            start_time="2026-01-17T19:00:00")
        pf.book_class(booking_id="BK3", session_id="SES3", member_id="M2",
                      package_id="PKG2", booked_at="2026-01-16T10:00:00")
        pf.check_in(booking_id="BK3", unit_amount_cents=20000,
                    recorded_at="2026-01-17T20:00:00")

        rows = {r["store_id"]: r for r in pf.store_comparison()["stores"]}
        s1, s2 = rows["S1"], rows["S2"]
        self.assertEqual(s1["amounts"]["CNY"]["gross_revenue_cents"], 20000)
        self.assertEqual(s1["amounts"]["CNY"]["retained_cents"], 20000 - 6000)
        self.assertEqual(s2["amounts"]["CNY"]["gross_revenue_cents"], 40000)
        self.assertEqual(s2["amounts"]["CNY"]["retained_cents"], 40000 - 12000)
        # 人效：S2 一名教练产出两场，人均留存更高
        self.assertEqual(s1["active_coaches"], 1)
        self.assertEqual(s2["active_coaches"], 1)
        self.assertEqual(s2["checked_in_sessions"], 2)
        self.assertEqual(
            s2["efficiency"]["CNY"]["gross_revenue_per_session_cents"], 20000
        )
        self.assertGreater(
            s2["efficiency"]["CNY"]["retained_per_coach_cents"],
            s1["efficiency"]["CNY"]["retained_per_coach_cents"],
        )
        # 按留存降序
        ordered = [r["store_id"] for r in pf.store_comparison()["stores"]]
        self.assertEqual(ordered, ["S2", "S1"])

    def test_statement_shows_sources_and_links(self) -> None:
        """教练端逐笔收入带来源、规则版本与关联记录。"""
        pf = make_platform()
        seed_class(pf, referral=True, sales_owner_id="SALES-A")
        pf.settle_month("2026-01")
        st = pf.coach_statement("C-LI")
        row = st["entries"][0]
        self.assertEqual(row["source"], "授课提成")
        self.assertEqual(row["rule_version"], "v2026-01")
        self.assertEqual(row["linked_record_id"], "BK1")
        self.assertEqual(row["batch_no"], st["entries"][0]["batch_no"])
        self.assertIsNotNone(row["event_id"])

        sales = pf.coach_statement("SALES-A")
        self.assertEqual(sales["breakdown_cents"]["referral"], 5000)


if __name__ == "__main__":
    unittest.main()
