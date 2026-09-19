"""分润推导引擎：从事件台账纯函数地推导分润分录。

设计要点
--------
* 分录(entry) ID 由其来源事件序号与事件内序号确定性派生
  （E000123-01），财务重跑同一批次时编号与金额保持稳定。
* 一切"修改"都是新增的负向分录：冲销、退款、坏账均链接回原分录
  （linked_entry_id），原分录永不删除。
* 收入责任落在真实服务发生处：核销时按当时排课快照里的教练与门店
  记账；课包转让只影响转让之后的核销。
* 规则版本按"服务发生日"选取；拉新奖励在成交日一次性确认。
"""
from __future__ import annotations

from typing import Any, Optional

from domain import (
    KIND_COMMISSION,
    KIND_DEFERRED,
    KIND_REFERRAL,
    KIND_REVENUE,
    period_of,
)
from ledger import Event
from money import ratio_amount, split_price, to_rate


class _SaleAcc:
    __slots__ = (
        "price", "sessions", "service_store", "salesperson", "referred",
        "schedule", "slots", "referral_entry", "referral_rate", "sale_date",
    )

    def __init__(self, payload: dict[str, Any]) -> None:
        self.price = payload["price_cents"]
        self.sessions = payload["sessions"]
        self.service_store = payload["store_id"]
        self.salesperson = payload.get("salesperson_id")
        self.referred = payload.get("referred", False)
        self.schedule = split_price(self.price, self.sessions)
        # index -> 占用该课次槽位的签到信息
        self.slots: dict[int, dict[str, Any]] = {}
        self.referral_entry: Optional[str] = None
        self.referral_rate: Optional[str] = None
        self.sale_date = payload["sale_date"]


def _rule_at(rules: list[dict[str, Any]], date_str: str) -> dict[str, Any]:
    chosen = None
    for rule in sorted(rules, key=lambda r: r["effective_from"]):
        if rule["effective_from"] <= date_str:
            chosen = rule
    return chosen  # type: ignore[return-value]


def _entry(
    event: Event, idx: int, period: str, kind: str, amount: int,
    *, settleable: bool, source: str, store_id: Optional[str] = None,
    person_id: Optional[str] = None, linked_entry_id: Optional[str] = None,
    correction_batch_id: Optional[str] = None, trace: str = "",
) -> dict[str, Any]:
    return {
        "entry_id": f"E{event.seq:06d}-{idx:02d}",
        "seq": event.seq,
        "source_event_id": event.id,
        "linked_entry_id": linked_entry_id,
        "period": period,
        "kind": kind,
        "person_id": person_id,
        "store_id": store_id,
        "amount_cents": amount,
        "settleable": settleable,
        "source": source,
        "correction_batch_id": correction_batch_id,
        "trace": trace,
    }


def compute_entries(events: list[Event]) -> list[dict[str, Any]]:
    """把台账事件流确定性地展开为分润分录列表。"""
    rules: list[dict[str, Any]] = []
    sales: dict[str, _SaleAcc] = {}
    sessions: dict[str, dict[str, Any]] = {}
    # checkin_id -> 原始核销产生的分录ID（commission, revenue, deferred）
    checkin_entries: dict[str, list[str]] = {}
    entries: list[dict[str, Any]] = []

    def add(**kwargs: Any) -> dict[str, Any]:
        ent = _entry(event=e, idx=len(entries_in_event) + 1, **kwargs)
        entries_in_event.append(ent)
        return ent

    for e in events:
        p = e.payload
        entries_in_event: list[dict[str, Any]] = []

        if e.type == "rule_version_published":
            rules.append(p)

        elif e.type == "class_session_created":
            sessions[p["session_id"]] = {
                "coach_id": p["coach_id"],
                "store_id": p["store_id"],
                "scheduled_date": p["scheduled_date"],
            }

        elif e.type == "coach_reassigned":
            # 只影响此后核销；已发生的服务归属不变。
            sessions[p["session_id"]]["coach_id"] = p["new_coach_id"]

        elif e.type == "package_sold":
            acc = _SaleAcc(p)
            sales[p["sale_id"]] = acc
            period = period_of(p["sale_date"])
            batch = p.get("correction_batch_id")
            # 成交即收款，但服务尚未发生：计入递延，不算任何人的应得。
            add(
                period=period, kind=KIND_DEFERRED, amount=p["price_cents"],
                settleable=False, source="package_sale",
                store_id=p["store_id"], correction_batch_id=batch,
                trace=f"课包 {p['package_id']} {p['sessions']}次 成交递延",
            )
            # 拉新奖励：首单一次性确认，与后续授课提成天然分离，不会重复计课。
            if acc.referred and acc.salesperson:
                rule = _rule_at(rules, p["sale_date"])
                if rule and p["price_cents"] >= rule.get("referral_qualifying_min_cents", 0):
                    rate = to_rate(rule["referral_rate"])
                    acc.referral_rate = str(rate)
                    bonus = ratio_amount(p["price_cents"], rate)
                    ent = add(
                        period=period, kind=KIND_REFERRAL, amount=bonus,
                        settleable=True, source="package_sale",
                        person_id=acc.salesperson,
                        store_id=p["store_id"], correction_batch_id=batch,
                        trace=f"会员 {p['member_id']} 首单拉新奖励",
                    )
                    acc.referral_entry = ent["entry_id"]

        elif e.type == "check_in_recorded":
            acc = sales[p["sale_id"]]
            idx = p["redemption_index"]
            service_value = acc.schedule[idx]
            service_period = period_of(p["service_date"])
            rule = _rule_at(rules, p["service_date"])
            commission = ratio_amount(service_value, to_rate(rule["commission_rate"]))
            batch = p.get("correction_batch_id")
            origin = "签到补录" if p.get("source") == "backfill" else "团课签到核销"
            trace = f"{origin}：课包第 {idx + 1}/{acc.sessions} 次"
            if p.get("reason"):
                trace += f"（理由：{p['reason']}）"
            c_ent = add(
                period=service_period, kind=KIND_COMMISSION, amount=commission,
                settleable=True, source=p.get("source", "normal"),
                person_id=p["coach_id"], store_id=p["store_id"],
                correction_batch_id=batch,
                trace=trace + f"，授课教练 {p['coach_id']}",
            )
            add(
                period=service_period, kind=KIND_REVENUE,
                amount=service_value - commission,
                settleable=True, source=p.get("source", "normal"),
                store_id=p["store_id"], correction_batch_id=batch,
                trace=trace + f"，服务门店 {p['store_id']} 留存",
            )
            add(
                period=service_period, kind=KIND_DEFERRED, amount=-service_value,
                settleable=False, source=p.get("source", "normal"),
                store_id=p["store_id"], correction_batch_id=batch,
                trace=trace + "，递延转收入",
            )
            acc.slots[idx] = {
                "checkin_id": p["checkin_id"], "value": service_value,
                "commission": commission, "coach_id": p["coach_id"],
                "store_id": p["store_id"], "service_date": p["service_date"],
                "source": p.get("source", "normal"),
            }
            checkin_entries[p["checkin_id"]] = [
                c_ent["entry_id"], entries_in_event[1]["entry_id"],
                entries_in_event[2]["entry_id"],
            ]

        elif e.type == "check_in_reversed":
            cid = p["checkin_id"]
            # 通过原签到记录关联，反向分录逐笔链接原分录。
            slot_sale: Optional[str] = None
            slot_idx: Optional[int] = None
            for sid, acc in sales.items():
                for i, slot in acc.slots.items():
                    if slot["checkin_id"] == cid:
                        slot_sale, slot_idx = sid, i
            if slot_sale is None:
                continue  # 台账不允许出现这种情况（Service 层已校验）
            acc = sales[slot_sale]
            slot = acc.slots.pop(slot_idx)
            period = period_of(slot["service_date"])
            batch = p.get("correction_batch_id")
            ids = checkin_entries.get(cid, [None, None, None])
            reason = p.get("reason", "")
            add(
                period=period, kind=KIND_COMMISSION, amount=-slot["commission"],
                settleable=True, source="checkin_reversal",
                person_id=slot["coach_id"], linked_entry_id=ids[0],
                correction_batch_id=batch,
                trace=f"冲销签到 {cid} 的授课提成（理由：{reason}）",
            )
            add(
                period=period, kind=KIND_REVENUE,
                amount=-(slot["value"] - slot["commission"]),
                settleable=True, source="checkin_reversal",
                store_id=slot["store_id"], linked_entry_id=ids[1],
                correction_batch_id=batch,
                trace=f"冲销签到 {cid} 的门店收入（原因：{reason}）",
            )
            add(
                period=period, kind=KIND_DEFERRED, amount=slot["value"],
                settleable=False, source="checkin_reversal",
                store_id=slot["store_id"], linked_entry_id=ids[2],
                correction_batch_id=batch,
                trace=f"冲销签到 {cid}，收入转回递延",
            )

        elif e.type == "package_transferred":
            # 转让本身不产生分录：历史收入留在原服务门店，未来核销快照新门店。
            if p.get("to_store_id"):
                sales[p["sale_id"]].service_store = p["to_store_id"]

        elif e.type in ("refund_recorded", "bad_debt_written_off"):
            source = "refund" if e.type == "refund_recorded" else "bad_debt"
            acc = sales[p["sale_id"]]
            amount = p["amount_cents"]
            period = period_of(p["date"])
            batch = p.get("correction_batch_id")
            label = "退款" if source == "refund" else "坏账核销"
            # 1) 先冲减仍有效的已履约课次（按课次顺序确定，不依赖字典遍历）。
            remaining = amount
            for i in sorted(acc.slots):
                if remaining <= 0:
                    break
                slot = acc.slots[i]
                value = min(slot["value"], remaining)
                rule = _rule_at(rules, slot["service_date"])
                commission = ratio_amount(value, to_rate(rule["commission_rate"]))
                ids = checkin_entries.get(slot["checkin_id"], [None, None, None])
                add(
                    period=period, kind=KIND_COMMISSION, amount=-commission,
                    settleable=True, source=source,
                    person_id=slot["coach_id"], linked_entry_id=ids[0],
                    correction_batch_id=batch,
                    trace=f"{label}冲减授课 {value}分：{slot['checkin_id']}（{p['reason']}）",
                )
                add(
                    period=period, kind=KIND_REVENUE, amount=-(value - commission),
                    settleable=True, source=source,
                    store_id=slot["store_id"], linked_entry_id=ids[1],
                    correction_batch_id=batch,
                    trace=f"{label}冲减门店收入 {value}分（{p['reason']}）",
                )
                remaining -= value
            # 2) 未能归到已履约课次的部分，冲减未履约递延。
            if remaining > 0:
                add(
                    period=period, kind=KIND_DEFERRED, amount=-remaining,
                    settleable=False, source=source,
                    store_id=acc.service_store, correction_batch_id=batch,
                    trace=f"{label}冲减未履约递延（{p['reason']}）",
                )
            # 3) 已发放的拉新奖励按比例追回（链接原奖励分录）。
            if acc.referral_entry and acc.referral_rate:
                bonus_claw = ratio_amount(amount, to_rate(acc.referral_rate))
                if bonus_claw:
                    add(
                        period=period, kind=KIND_REFERRAL, amount=-bonus_claw,
                        settleable=True, source=source,
                        person_id=acc.salesperson,
                        linked_entry_id=acc.referral_entry,
                        correction_batch_id=batch,
                        trace=f"{label}追回拉新奖励（{p['reason']}）",
                    )

        elif e.type == "settlement_locked":
            pass  # 锁定本身不产生分录

        elif e.type == "correction_posted":
            batch_id = p["correction_batch_id"]
            for adj in p.get("adjustments", []):
                amount = adj["amount_cents"]
                if adj["target_type"] == "person":
                    add(
                        period=p["period"], kind=adj.get("kind", "manual_adjustment"),
                        amount=amount, settleable=True, source="manual_adjustment",
                        person_id=adj["target_id"], correction_batch_id=batch_id,
                        trace=f"更正批次手工调整：{adj.get('reason', p['reason'])}",
                    )
                else:
                    add(
                        period=p["period"], kind=adj.get("kind", "manual_adjustment"),
                        amount=amount, settleable=True, source="manual_adjustment",
                        store_id=adj["target_id"], correction_batch_id=batch_id,
                        trace=f"更正批次手工调整：{adj.get('reason', p['reason'])}",
                    )

        entries.extend(entries_in_event)
    return entries
