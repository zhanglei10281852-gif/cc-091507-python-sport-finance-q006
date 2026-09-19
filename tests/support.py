"""测试公共构造：搭一个带规则版本的平台。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profitshare.rules import RuleVersion
from profitshare.service import Platform


def make_platform() -> Platform:
    pf = Platform()
    # 2026-01-01 起：授课提成 30%，拉新奖励 50 元
    pf.publish_rule(
        RuleVersion(
            "v2026-01",
            "2026-01-01",
            coach_bps=3000,
            referral_reward_cents=5000,
        )
    )
    return pf


def make_two_rule_platform() -> Platform:
    """带两个规则版本的平台：2 月起提成改为 40%、拉新 60 元。"""
    pf = Platform()
    pf.publish_rule(
        RuleVersion("v2026-01", "2026-01-01", coach_bps=3000, referral_reward_cents=5000)
    )
    pf.publish_rule(
        RuleVersion("v2026-02", "2026-02-01", coach_bps=4000, referral_reward_cents=6000)
    )
    return pf


def seed_class(
    pf: Platform,
    *,
    store_id: str = "S1",
    coach_id: str = "C-LI",
    member_id: str = "M1",
    package_id: str = "PKG1",
    session_id: str = "SES1",
    booking_id: str = "BK1",
    sales_owner_id: str | None = "SALES-A",
    unit_price: int = 20000,
    sold_at: str = "2026-01-02T10:00:00",
    class_time: str = "2026-01-10T19:00:00",
    referral: bool = False,
    total_amount_cents: int = 200000,
    total_qty: int = 10,
) -> dict:
    """购课包 + 排课 + 预约 + 签到，返回各 id。"""
    pf.purchase_package(
        package_id=package_id,
        member_id=member_id,
        store_id=store_id,
        total_qty=total_qty,
        total_amount_cents=total_amount_cents,
        sales_owner_id=sales_owner_id,
        sold_at=sold_at,
    )
    if referral:
        pf.register_referral(
            referral_id=f"REF-{member_id}",
            referred_member_id=member_id,
            sales_owner_id=sales_owner_id or "SALES-A",
            store_id=store_id,
            occurred_at=sold_at,
        )
    pf.schedule_session(
        session_id=session_id,
        store_id=store_id,
        coach_id=coach_id,
        start_time=class_time,
    )
    pf.book_class(
        booking_id=booking_id,
        session_id=session_id,
        member_id=member_id,
        package_id=package_id,
        booked_at=class_time[:11] + "10:00:00",
    )
    pf.check_in(
        booking_id=booking_id,
        unit_amount_cents=unit_price,
        recorded_at=class_time,
    )
    return {
        "package_id": package_id,
        "session_id": session_id,
        "booking_id": booking_id,
        "checkin_event_id": f"checkin:{booking_id}",
    }
