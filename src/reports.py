"""查询与报表：教练收入明细、门店留存/人效对比、月结批次报告。

所有数字都是 engine.compute_entries 的确定性聚合，不另存可变状态。
"""
from __future__ import annotations

from typing import Any, Optional

from domain import (
    KIND_COMMISSION,
    KIND_DEFERRED,
    KIND_REFERRAL,
    KIND_REVENUE,
)
from engine import compute_entries
from ledger import Ledger
from projection import Service


class Reports:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.service = Service(ledger)

    def _data(self):
        st = self.service.state()
        entries = compute_entries(self.ledger.events)
        return st, entries

    # --------------------------------------------------------- 教练端查询
    def coach_earnings(
        self, person_id: str, period: Optional[str] = None,
        include_corrections: bool = True,
    ) -> dict[str, Any]:
        st, entries = self._data()
        person = st.people.get(person_id)
        if not person:
            from domain import NotFoundError
            raise NotFoundError(f"人员不存在: {person_id}")
        rows = []
        for ent in entries:
            if ent["person_id"] != person_id or not ent["settleable"]:
                continue
            if period and ent["period"] != period:
                continue
            if not include_corrections and ent["correction_batch_id"]:
                continue
            rows.append(self._row(ent))
        earned = sum(r["amount_cents"] for r in rows)
        gross_positive = sum(r["amount_cents"] for r in rows if r["amount_cents"] > 0)
        deductions = -sum(r["amount_cents"] for r in rows if r["amount_cents"] < 0)
        return {
            "person_id": person_id, "name": person["name"], "role": person["role"],
            "period": period,
            "total_cents": earned,
            "gross_cents": gross_positive,
            "deductions_cents": deductions,
            "entries": rows,
        }

    @staticmethod
    def _row(ent: dict[str, Any]) -> dict[str, Any]:
        return {
            "entry_id": ent["entry_id"],
            "period": ent["period"],
            "kind": ent["kind"],
            "source": ent["source"],
            "amount_cents": ent["amount_cents"],
            "direction": "credit" if ent["amount_cents"] >= 0 else "debit",
            "linked_entry_id": ent["linked_entry_id"],
            "correction_batch_id": ent["correction_batch_id"],
            "origin_event_id": ent["source_event_id"],
            "description": ent["trace"],
        }

    # --------------------------------------------------------- 门店对比
    def store_comparison(self, period: str) -> dict[str, Any]:
        from domain import parse_period
        parse_period(period)
        st, entries = self._data()
        result: dict[str, dict[str, Any]] = {
            sid: {
                "store_id": sid, "name": info["name"],
                "revenue_cents": 0, "commission_cents": 0,
                "referral_cents": 0, "sessions_served": 0,
                "coaches": set(),
            }
            for sid, info in st.stores.items()
        }
        served: dict[str, set[str]] = {sid: set() for sid in st.stores}
        for ent in entries:
            if ent["period"] != period or not ent["settleable"]:
                continue
            sid = ent["store_id"]
            if sid not in result:
                continue
            if ent["kind"] == KIND_REVENUE:
                result[sid]["revenue_cents"] += ent["amount_cents"]
                # 已服务课次按来源事件去重（反向分录不重复计数）。
                if ent["amount_cents"] >= 0 and ent["source"] != "checkin_reversal":
                    served[sid].add(ent["source_event_id"])
            elif ent["kind"] == KIND_COMMISSION:
                result[sid]["commission_cents"] += ent["amount_cents"]
                if ent["amount_cents"] >= 0 and ent["person_id"]:
                    result[sid]["coaches"].add(ent["person_id"])
            elif ent["kind"] == KIND_REFERRAL:
                result[sid]["referral_cents"] += ent["amount_cents"]
        rows = []
        for sid, agg in result.items():
            agg["sessions_served"] = len(served[sid])
            coach_count = len(agg["coaches"])
            recognized = agg["revenue_cents"] + agg["commission_cents"]
            agg.pop("coaches")
            agg["recognized_service_cents"] = recognized
            # 留存率：门店留存占已确认服务收入的比例（含冲销后的净额）。
            agg["retention_rate"] = (
                round(agg["revenue_cents"] / recognized, 6) if recognized > 0 else None
            )
            # 人效：每位授课教练对应的门店留存。
            agg["revenue_per_coach_cents"] = (
                round(agg["revenue_cents"] / coach_count) if coach_count else None
            )
            agg["active_coaches"] = coach_count
            rows.append(agg)
        rows.sort(key=lambda r: r["store_id"])
        return {"period": period, "stores": rows}

    # --------------------------------------------------------- 月结批次
    def settlement_batch(self, period: str, currency: str = "CNY") -> dict[str, Any]:
        from domain import parse_period
        parse_period(period)
        st, entries = self._data()
        lock = st.locked_periods.get(period)
        if not lock:
            from domain import NotFoundError
            raise NotFoundError(f"期间 {period} 尚未锁定")
        head_seq = lock["head_seq"]
        batch_id = lock["batch_id"]
        # 锁定快照：仅统计锁定台账序号之前、且不属于任何更正批次的分录。
        snapshot = [
            ent for ent in entries
            if ent["period"] == period
            and ent["seq"] <= head_seq
            and not ent["correction_batch_id"]
        ]
        return self._batch_summary(batch_id, period, currency, snapshot, lock, st)

    def correction_batch(self, batch_id: str) -> dict[str, Any]:
        st, entries = self._data()
        batch = st.correction_batches.get(batch_id)
        if not batch:
            from domain import NotFoundError
            raise NotFoundError(f"更正批次不存在: {batch_id}")
        rows = [ent for ent in entries if ent["correction_batch_id"] == batch_id]
        summary = self._batch_summary(
            batch_id, batch["period"],
            st.locked_periods[batch["period"]]["currency"], rows, batch, st,
        )
        summary["reason"] = batch["reason"]
        summary["status"] = batch["status"]
        return summary

    def _batch_summary(
        self, batch_id: str, period: str, currency: str,
        rows: list[dict[str, Any]], meta: dict[str, Any], st: Any,
    ) -> dict[str, Any]:
        by_person: dict[str, int] = {}
        by_store: dict[str, int] = {}
        deferred = 0
        for ent in rows:
            if ent["kind"] == KIND_DEFERRED or not ent["settleable"]:
                deferred += ent["amount_cents"]
            if ent["person_id"] and ent["settleable"]:
                by_person[ent["person_id"]] = by_person.get(ent["person_id"], 0) + ent["amount_cents"]
            # 门店"应得"只含留存收入；教练提成/拉新奖励虽带门店归属，
            # 但属于个人款项，不能重复计入门店。
            if ent["store_id"] and ent["kind"] == KIND_REVENUE:
                by_store[ent["store_id"]] = by_store.get(ent["store_id"], 0) + ent["amount_cents"]
        people_detail = []
        for pid, amount in sorted(by_person.items()):
            info = st.people.get(pid, {"name": "?"})
            people_detail.append({
                "person_id": pid, "name": info.get("name"),
                "net_cents": amount, "store_id": info.get("store_id"),
            })
        stores_detail = [
            {"store_id": sid, "name": st.stores.get(sid, {}).get("name"), "net_cents": amount}
            for sid, amount in sorted(by_store.items())
        ]
        entry_rows = [self._row(ent) for ent in rows]
        entry_rows.sort(key=lambda r: r["entry_id"])
        return {
            "batch_id": batch_id,
            "period": period,
            "currency": currency,
            "status": meta.get("status", "locked"),
            "entry_count": len(entry_rows),
            "person_totals_cents": sum(by_person.values()),
            "store_totals_cents": sum(by_store.values()),
            "deferred_delta_cents": deferred,
            "people": people_detail,
            "stores": stores_detail,
            "entries": entry_rows,
        }
