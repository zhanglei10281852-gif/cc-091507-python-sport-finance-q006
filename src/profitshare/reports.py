"""只读查询模型。

- coach_statement：教练/销售视角的逐笔收入与扣减，标注来源与关联记录；
- store_comparison：店长按门店比较收入、提成支出、留存与教练人效。

全部基于 MaterializedView 的分录计算，不产生任何写入。
"""

from __future__ import annotations

from decimal import Decimal

from .engine import (
    PAYEE_COACH,
    PAYEE_SALES,
    PAYEE_STORE,
    SRC_BAD_DEBT_REVERSAL,
    SRC_COACH_COMMISSION,
    SRC_CORRECTION,
    SRC_REFERRAL,
    SRC_REFUND_REVERSAL,
    SRC_REVENUE,
    Entry,
    MaterializedView,
)

PERSONAL_SOURCES = (
    SRC_COACH_COMMISSION,
    SRC_REFERRAL,
    SRC_REFUND_REVERSAL,
    SRC_BAD_DEBT_REVERSAL,
    SRC_CORRECTION,
)


def _entry_row(e: Entry) -> dict:
    return {
        "entry_id": e.entry_id,
        "period": e.period,
        "source": e.source,
        "amount_cents": e.amount_cents,
        "currency": e.currency,
        "rule_version": e.rule_version,
        "memo": e.memo,
        "reversal_of": e.reversal_of,
        "batch_no": e.batch_no,
        "linked_record_id": e.linked_record_id,
        "event_id": e.event_id,
    }


def coach_statement(view: MaterializedView, person_id: str) -> dict:
    """个人收入台账：正数为收入，负数为扣减，逐笔可溯源。

    金额按币种分组（同一人可能在不同币种的门店授课），绝不混加。
    """
    rows = [
        _entry_row(e)
        for e in view.entries_by_person(person_id)
        if e.source in PERSONAL_SOURCES
    ]
    rows.sort(key=lambda r: (r["period"], r["entry_id"]))

    by_ccy: dict[str, dict] = {}
    for r in rows:
        bucket = by_ccy.setdefault(
            r["currency"],
            {"earned_cents": 0, "deductions_cents": 0, "net_cents": 0,
             "breakdown_cents": {"commission": 0, "referral": 0,
                                 "reversal": 0, "correction": 0}},
        )
        if r["amount_cents"] >= 0:
            bucket["earned_cents"] += r["amount_cents"]
        else:
            bucket["deductions_cents"] += r["amount_cents"]
        bucket["net_cents"] += r["amount_cents"]
        key = {
            SRC_COACH_COMMISSION: "commission",
            SRC_REFERRAL: "referral",
            SRC_REFUND_REVERSAL: "reversal",
            SRC_BAD_DEBT_REVERSAL: "reversal",
            SRC_CORRECTION: "correction",
        }[r["source"]]
        bucket["breakdown_cents"][key] += r["amount_cents"]

    primary_ccy = rows[0]["currency"] if rows else None
    primary = by_ccy.get(primary_ccy, {}) if primary_ccy else {}

    return {
        "person_id": person_id,
        "currencies": sorted(by_ccy),
        # 顶层保留单币种便捷字段（本工作室主币种 CNY 场景）
        "currency": primary_ccy,
        "earned_cents": primary.get("earned_cents", 0),
        "deductions_cents": primary.get("deductions_cents", 0),
        "net_cents": primary.get("net_cents", 0),
        "breakdown_cents": primary.get(
            "breakdown_cents",
            {"commission": 0, "referral": 0, "reversal": 0, "correction": 0},
        ),
        "by_currency": by_ccy,
        "entries": rows,
    }


def _store_entries(view: MaterializedView, store_id: str) -> list[Entry]:
    return [e for e in view.entries if e.store_id == store_id]


def _amounts_by_currency(entries, predicate) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        if predicate(e):
            out[e.currency] = out.get(e.currency, 0) + e.amount_cents
    return out


def store_statement(view: MaterializedView, store_id: str) -> dict:
    entries = _store_entries(view, store_id)

    # 按币种、收款方汇总（各自的反向分录/更正天然带符号并入），不同币种不混加
    gross_by_ccy = _amounts_by_currency(
        entries,
        lambda e: e.payee_type == PAYEE_STORE
        and e.source == SRC_REVENUE
        and e.amount_cents > 0,
    )
    net_by_ccy = _amounts_by_currency(
        entries, lambda e: e.payee_type == PAYEE_STORE
    )
    coach_by_ccy = _amounts_by_currency(
        entries, lambda e: e.payee_type == PAYEE_COACH
    )
    sales_by_ccy = _amounts_by_currency(
        entries, lambda e: e.payee_type == PAYEE_SALES
    )
    currencies = sorted(
        set(gross_by_ccy) | set(net_by_ccy)
        | set(coach_by_ccy) | set(sales_by_ccy)
    )
    amounts: dict[str, dict] = {}
    for ccy in currencies:
        net = net_by_ccy.get(ccy, 0)
        gross = gross_by_ccy.get(ccy, 0)
        retained = net - coach_by_ccy.get(ccy, 0) - sales_by_ccy.get(ccy, 0)
        amounts[ccy] = {
            "gross_revenue_cents": gross,
            "net_revenue_cents": net,
            "payouts_coach_cents": coach_by_ccy.get(ccy, 0),
            "payouts_sales_cents": sales_by_ccy.get(ccy, 0),
            "retained_cents": retained,
            "retention_ratio": (retained / gross) if gross > 0 else None,
        }

    # 人效：实际产生核销的教练与场次
    serving_coaches: set[str] = set()
    checked_in_sessions: set[str] = set()
    consumed_qty = Decimal("0")
    for booking in view.bookings.values():
        session = view.sessions.get(booking.session_id)
        if session is None or session.store_id != store_id:
            continue
        if booking.checkin_event_id is None:
            continue
        checked_in_sessions.add(booking.session_id)
        serving_coaches.add(session.actual_coach_id or session.coach_id)
        consumed_qty += booking.qty

    coach_count = len(serving_coaches)
    return {
        "store_id": store_id,
        "currencies": currencies,
        "amounts": amounts,
        "checked_in_sessions": len(checked_in_sessions),
        "consumed_qty": str(consumed_qty),
        "active_coaches": coach_count,
    }


def store_comparison(view: MaterializedView) -> dict:
    """跨门店对比。门店跨币种时不做单一排序键，先按门店 id 稳定输出，
    各币种留存与单场均值在 amounts / efficiency 内查看。"""
    store_ids = {
        *(pkg.store_id for pkg in view.packages.values()),
        *(s.store_id for s in view.sessions.values()),
        *(e.store_id for e in view.entries if e.payee_type == PAYEE_STORE),
    }
    rows = []
    for sid in sorted(s for s in store_ids if s):
        row = store_statement(view, sid)
        sessions = row["checked_in_sessions"]
        # 人效按币种给出
        efficiency: dict[str, dict] = {}
        for ccy, a in row["amounts"].items():
            efficiency[ccy] = {
                "retained_per_coach_cents": (
                    a["retained_cents"] // row["active_coaches"]
                    if row["active_coaches"]
                    else None
                ),
                "gross_revenue_per_session_cents": (
                    a["gross_revenue_cents"] // sessions if sessions else None
                ),
            }
        row["efficiency"] = efficiency
        # 单币种门店保留一个便于排序/展示的主留存（本工作室当前主币种 CNY）
        row["primary_retained_cents"] = (
            row["amounts"].get("CNY", {}).get("retained_cents")
        )
        rows.append(row)
    # 有 CNY 留存的按留存降序；无 CNY 数据的门店按 id 附后，保证顺序稳定
    rows.sort(
        key=lambda r: (
            0 if r["primary_retained_cents"] is not None else 1,
            -(r["primary_retained_cents"] or 0),
            r["store_id"],
        )
    )
    return {"stores": rows}
