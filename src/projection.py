"""状态投影与业务服务：回放不可变台账，校验命令后追加新事件。

事件一旦写入不可修改；所有"变更"（替补、退款、坏账、冲销、更正）
都是追加的新事实。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from domain import (
    ConflictError,
    DomainError,
    NotFoundError,
    cents,
    parse_date,
    period_of,
    require,
)
from ledger import Ledger, new_id


@dataclass
class RuleVersion:
    version_id: str
    effective_from: str
    commission_rate: str
    referral_rate: str
    qualifying_min_cents: int


@dataclass
class Sale:
    sale_id: str
    package_id: str
    member_id: str
    sessions: int
    price_cents: int
    store_id: str
    salesperson_id: Optional[str]
    referred: bool
    sale_date: str
    on_credit: bool
    owner_member_id: str = ""
    service_store_id: str = ""
    refunded_cents: int = 0
    bad_debt_cents: int = 0

    def __post_init__(self) -> None:
        if not self.owner_member_id:
            self.owner_member_id = self.member_id
        if not self.service_store_id:
            self.service_store_id = self.store_id


@dataclass
class Session:
    session_id: str
    class_type: str
    store_id: str
    scheduled_date: str
    coach_id: str


@dataclass
class Checkin:
    checkin_id: str
    booking_id: str
    session_id: str
    member_id: str
    sale_id: str
    redemption_index: int          # 核销的是课包第几次（0 起），创建时确定
    coach_id: str                  # 服务发生时的教练快照
    store_id: str                  # 服务发生门店快照
    source: str                    # normal / backfill
    service_date: str
    recorded_date: str
    reversed: bool = False
    post_lock: bool = False


@dataclass
class State:
    stores: dict[str, dict[str, Any]] = field(default_factory=dict)
    people: dict[str, dict[str, Any]] = field(default_factory=dict)
    rules: list[RuleVersion] = field(default_factory=list)
    sales: dict[str, Sale] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    bookings: dict[str, dict[str, Any]] = field(default_factory=dict)
    checkins: dict[str, Checkin] = field(default_factory=dict)
    locked_periods: dict[str, dict[str, Any]] = field(default_factory=dict)
    corrections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    member_first_sale: dict[str, str] = field(default_factory=dict)
    correction_requests: dict[str, str] = field(default_factory=dict)
    # batch_id -> 批次信息（opened / posted）
    correction_batches: dict[str, dict[str, Any]] = field(default_factory=dict)

    def rule_at(self, date_str: str) -> RuleVersion:
        chosen: Optional[RuleVersion] = None
        for rule in sorted(self.rules, key=lambda r: r.effective_from):
            if rule.effective_from <= date_str:
                chosen = rule
        if chosen is None:
            raise ConflictError(f"日期 {date_str} 没有已生效的提成规则版本")
        return chosen


class Service:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    # ------------------------------------------------------------------ 回放
    def state(self) -> State:
        st = State()
        st.sessions = {}
        # 每笔销售已占用/释放的课次槽位（用于确定性地分配核销序号）
        used_slots: dict[str, set[int]] = {}
        for e in self.ledger.events:
            p = e.payload
            t = e.type
            if t == "store_registered":
                st.stores[p["store_id"]] = {"name": p["name"]}
            elif t == "person_registered":
                st.people[p["person_id"]] = {
                    "name": p["name"], "role": p["role"], "store_id": p.get("store_id")
                }
            elif t == "rule_version_published":
                st.rules.append(
                    RuleVersion(
                        p["version_id"],
                        p["effective_from"],
                        p["commission_rate"],
                        p["referral_rate"],
                        p.get("referral_qualifying_min_cents", 0),
                    )
                )
            elif t == "package_sold":
                sale = Sale(
                    p["sale_id"], p["package_id"], p["member_id"], p["sessions"],
                    p["price_cents"], p["store_id"], p.get("salesperson_id"),
                    p.get("referred", False), p["sale_date"], p.get("on_credit", False),
                )
                st.sales[sale.sale_id] = sale
                st.member_first_sale.setdefault(sale.member_id, sale.sale_id)
                used_slots[sale.sale_id] = set()
            elif t == "class_session_created":
                st.sessions[p["session_id"]] = Session(
                    p["session_id"], p["class_type"], p["store_id"],
                    p["scheduled_date"], p["coach_id"],
                )
            elif t == "coach_reassigned":
                st.sessions[p["session_id"]].coach_id = p["new_coach_id"]
            elif t == "booking_registered":
                st.bookings[p["booking_id"]] = {
                    "session_id": p["session_id"], "member_id": p["member_id"],
                    "sale_id": p.get("sale_id"), "checked_in": False,
                }
            elif t == "check_in_recorded":
                session = st.sessions[p["session_id"]]
                ci = Checkin(
                    p["checkin_id"], p["booking_id"], p["session_id"], p["member_id"],
                    p["sale_id"], p["redemption_index"], p["coach_id"], p["store_id"],
                    p["source"], p["service_date"], p["recorded_date"],
                    post_lock=p.get("post_lock", False),
                )
                st.checkins[ci.checkin_id] = ci
                used_slots[p["sale_id"]].add(p["redemption_index"])
                st.bookings[p["booking_id"]]["checked_in"] = True
            elif t == "check_in_reversed":
                ci = st.checkins[p["checkin_id"]]
                ci.reversed = True
                used_slots[ci.sale_id].discard(ci.redemption_index)
                st.bookings[ci.booking_id]["checked_in"] = False
            elif t == "package_transferred":
                sale = st.sales[p["sale_id"]]
                sale.owner_member_id = p["new_member_id"]
                if p.get("to_store_id"):
                    sale.service_store_id = p["to_store_id"]
            elif t in ("refund_recorded", "bad_debt_written_off"):
                sale = st.sales[p["sale_id"]]
                key = "refunded_cents" if t == "refund_recorded" else "bad_debt_cents"
                setattr(sale, key, getattr(sale, key) + p["amount_cents"])
            elif t == "settlement_locked":
                st.locked_periods[p["period"]] = p
                st.corrections.setdefault(p["period"], [])
            elif t == "correction_opened":
                st.correction_batches[p["correction_batch_id"]] = {
                    "correction_batch_id": p["correction_batch_id"],
                    "period": p["period"], "reason": p["reason"],
                    "status": "open", "opened_at": p["opened_at"],
                    "adjustments": [],
                }
            elif t == "correction_posted":
                batch = st.correction_batches.get(p["correction_batch_id"])
                if batch:
                    batch["status"] = "posted"
                    batch["posted_at"] = p["posted_at"]
                    batch["adjustments"] = p.get("adjustments", [])
                st.corrections.setdefault(p["period"], []).append(p)
                if p.get("request_id"):
                    st.correction_requests[p["request_id"]] = p["correction_batch_id"]
        return st

    # ------------------------------------------------------------- 基础档案
    def register_store(self, name: str, store_id: str | None = None) -> dict[str, Any]:
        require(name, "门店名称不能为空")
        st = self.state()
        store_id = store_id or new_id("store")
        if store_id in st.stores:
            raise ConflictError(f"门店已存在: {store_id}")
        self.ledger.append("store_registered", {"store_id": store_id, "name": name})
        return {"store_id": store_id, "name": name}

    def register_person(
        self, name: str, role: str, person_id: str | None = None,
        store_id: str | None = None,
    ) -> dict[str, Any]:
        require(name, "姓名不能为空")
        if role not in ("coach", "sales", "both"):
            raise DomainError("role 必须是 coach / sales / both")
        st = self.state()
        if store_id and store_id not in st.stores:
            raise NotFoundError(f"门店不存在: {store_id}")
        person_id = person_id or new_id("psn")
        if person_id in st.people:
            raise ConflictError(f"人员已存在: {person_id}")
        self.ledger.append(
            "person_registered",
            {"person_id": person_id, "name": name, "role": role, "store_id": store_id},
        )
        return {"person_id": person_id, "name": name, "role": role}

    def publish_rule(
        self, effective_from: str, commission_rate: Any, referral_rate: Any,
        qualifying_min_cents: int = 0, version_id: str | None = None,
    ) -> dict[str, Any]:
        parse_date(effective_from)
        from money import to_rate
        cr = to_rate(commission_rate)
        rr = to_rate(referral_rate)
        st = self.state()
        version_id = version_id or new_id("rule")
        if any(r.version_id == version_id for r in st.rules):
            raise ConflictError(f"规则版本已存在: {version_id}")
        if any(r.effective_from == effective_from for r in st.rules):
            raise ConflictError(f"生效日 {effective_from} 已存在规则版本")
        payload = {
            "version_id": version_id,
            "effective_from": effective_from,
            "commission_rate": str(cr),
            "referral_rate": str(rr),
            "referral_qualifying_min_cents": int(qualifying_min_cents or 0),
        }
        self.ledger.append("rule_version_published", payload)
        return payload

    # ----------------------------------------------------------------- 销售
    def sell_package(self, data: dict[str, Any]) -> dict[str, Any]:
        st = self.state()
        sale_id = data.get("sale_id") or new_id("sale")
        package_id = require(data.get("package_id"), "缺少 package_id")
        member_id = require(data.get("member_id"), "缺少 member_id")
        sessions = data.get("sessions")
        if not isinstance(sessions, int) or sessions <= 0:
            raise DomainError("sessions 必须为正整数")
        price = cents(data.get("price_cents"), "price_cents")
        store_id = require(data.get("store_id"), "缺少 store_id")
        if store_id not in st.stores:
            raise NotFoundError(f"门店不存在: {store_id}")
        salesperson = data.get("salesperson_id")
        if salesperson and salesperson not in st.people:
            raise NotFoundError(f"销售顾问不存在: {salesperson}")
        sale_date = parse_date(data.get("sale_date") or "")
        st.rule_at(sale_date)  # 成交日必须有生效规则
        if sale_id in st.sales:
            raise ConflictError(f"销售单已存在: {sale_id}")
        referred = bool(data.get("referred", False))
        # 拉新奖励只对该会员的首单有效，杜绝把续费/重复购课包装成拉新。
        if referred and member_id in st.member_first_sale:
            raise ConflictError("该会员已有更早的成交单，拉新奖励仅限首单")
        batch_id = None
        if period_of(sale_date) in st.locked_periods:
            batch_id = self._require_open_batch(
                st, period_of(sale_date), data.get("correction_batch_id")
            )
        payload = {
            "sale_id": sale_id, "package_id": package_id, "member_id": member_id,
            "sessions": sessions, "price_cents": price, "store_id": store_id,
            "salesperson_id": salesperson, "referred": referred,
            "sale_date": sale_date, "on_credit": bool(data.get("on_credit", False)),
            "correction_batch_id": batch_id,
        }
        self.ledger.append("package_sold", payload)
        return payload

    # ----------------------------------------------------------------- 排课
    def create_session(self, data: dict[str, Any]) -> dict[str, Any]:
        st = self.state()
        session_id = data.get("session_id") or new_id("cls")
        store_id = require(data.get("store_id"), "缺少 store_id")
        coach_id = require(data.get("coach_id"), "缺少 coach_id")
        if store_id not in st.stores:
            raise NotFoundError(f"门店不存在: {store_id}")
        person = st.people.get(coach_id)
        if not person or person["role"] not in ("coach", "both"):
            raise NotFoundError(f"教练不存在: {coach_id}")
        scheduled = parse_date(data.get("scheduled_date") or "")
        class_type = require(data.get("class_type"), "缺少 class_type")
        if session_id in st.sessions:
            raise ConflictError(f"课程已存在: {session_id}")
        payload = {
            "session_id": session_id, "class_type": class_type,
            "store_id": store_id, "scheduled_date": scheduled, "coach_id": coach_id,
        }
        self.ledger.append("class_session_created", payload)
        return payload

    def substitute_coach(self, session_id: str, new_coach_id: str, reason: str) -> dict[str, Any]:
        """教练请假/替补：仅影响此后核销的签到，历史服务归属不变。"""
        require(reason, "替补必须填写原因（如教练请假）")
        st = self.state()
        if session_id not in st.sessions:
            raise NotFoundError(f"课程不存在: {session_id}")
        person = st.people.get(new_coach_id)
        if not person or person["role"] not in ("coach", "both"):
            raise NotFoundError(f"替补教练不存在: {new_coach_id}")
        payload = {
            "session_id": session_id, "new_coach_id": new_coach_id, "reason": reason,
        }
        self.ledger.append("coach_reassigned", payload)
        return payload

    # ------------------------------------------------------- 预约 / 签到核销
    def create_booking(self, data: dict[str, Any]) -> dict[str, Any]:
        st = self.state()
        session_id = require(data.get("session_id"), "缺少 session_id")
        member_id = require(data.get("member_id"), "缺少 member_id")
        if session_id not in st.sessions:
            raise NotFoundError(f"课程不存在: {session_id}")
        sale_id = data.get("sale_id")
        if sale_id is not None:
            sale = st.sales.get(sale_id)
            if not sale:
                raise NotFoundError(f"课包销售单不存在: {sale_id}")
            if sale.owner_member_id != member_id:
                raise ConflictError("课包当前不属于该会员（可能已转让）")
        booking_id = data.get("booking_id") or new_id("bkg")
        if booking_id in st.bookings:
            raise ConflictError(f"预约已存在: {booking_id}")
        for b in st.bookings.values():
            if b["session_id"] == session_id and b["member_id"] == member_id:
                raise ConflictError("该会员已预约这节课")
        payload = {
            "booking_id": booking_id, "session_id": session_id,
            "member_id": member_id, "sale_id": sale_id,
        }
        self.ledger.append("booking_registered", payload)
        return payload

    def check_in(self, data: dict[str, Any]) -> dict[str, Any]:
        st = self.state()
        booking_id = require(data.get("booking_id"), "缺少 booking_id")
        booking = st.bookings.get(booking_id)
        if not booking:
            raise NotFoundError(f"预约记录不存在: {booking_id}——补录也必须关联原始预约")
        if booking["checked_in"]:
            raise ConflictError("该预约已有有效签到，不能重复核销")
        sale_id = require(booking.get("sale_id"), "预约未关联课包，无法核销")
        sale = st.sales[sale_id]
        session = st.sessions[booking["session_id"]]
        st.rule_at(session.scheduled_date)  # 服务日必须有生效规则版本
        # 课次槽位：冲销后释放的槽位可被再次核销，顺序确定。
        used = {
            ci.redemption_index
            for ci in st.checkins.values()
            if ci.sale_id == sale_id and not ci.reversed
        }
        if len(used) >= sale.sessions:
            raise ConflictError("课包剩余次数不足，无法核销")
        index = next(i for i in range(sale.sessions) if i not in used)

        source = data.get("source", "normal")
        if source not in ("normal", "backfill"):
            raise DomainError("source 必须是 normal 或 backfill")
        recorded_date = parse_date(data.get("recorded_date") or session.scheduled_date)
        if source == "backfill" and recorded_date < session.scheduled_date:
            raise DomainError("补录日期不能早于课程日期")
        # 月结锁定后补录必须携带理由，并挂到一个开启中的更正批次。
        period = period_of(session.scheduled_date)
        post_lock = period in st.locked_periods
        reason = data.get("reason")
        batch_id: str | None = None
        if post_lock:
            if not reason:
                raise ConflictError(f"{period} 已月结锁定，补录必须填写理由并走更正批次")
            batch_id = self._require_open_batch(st, period, data.get("correction_batch_id"))

        checkin_id = data.get("checkin_id") or new_id("chk")
        if checkin_id in st.checkins:
            raise ConflictError(f"签到已存在: {checkin_id}")
        payload = {
            "checkin_id": checkin_id, "booking_id": booking_id,
            "session_id": session.session_id, "member_id": booking["member_id"],
            "sale_id": sale_id, "redemption_index": index,
            "coach_id": session.coach_id, "store_id": session.store_id,
            "source": source, "service_date": session.scheduled_date,
            "recorded_date": recorded_date, "post_lock": post_lock,
            "correction_batch_id": batch_id,
        }
        if post_lock:
            payload["reason"] = reason
        self.ledger.append("check_in_recorded", payload)
        return payload

    @staticmethod
    def _require_open_batch(st: State, period: str, batch_id: str | None) -> str:
        require(batch_id, "该期间已锁定，必须提供 correction_batch_id（更正批次）")
        batch = st.correction_batches.get(batch_id)
        if not batch:
            raise NotFoundError(f"更正批次不存在: {batch_id}")
        if batch["period"] != period:
            raise ConflictError(f"更正批次 {batch_id} 不属于期间 {period}")
        if batch["status"] != "open":
            raise ConflictError(f"更正批次 {batch_id} 已过账，不能再追加")
        return batch_id

    def reverse_checkin(
        self, checkin_id: str, reason: str, correction_batch_id: str | None = None
    ) -> dict[str, Any]:
        """签到冲销：必须关联原签到，释放课次，分润由反向分录冲回。"""
        require(reason, "冲销必须填写原因")
        st = self.state()
        ci = st.checkins.get(checkin_id)
        if not ci:
            raise NotFoundError(f"原签到记录不存在: {checkin_id}")
        if ci.reversed:
            raise ConflictError("该签到已被冲销")
        batch_id = None
        if period_of(ci.service_date) in st.locked_periods:
            batch_id = self._require_open_batch(
                st, period_of(ci.service_date), correction_batch_id
            )
        payload = {
            "checkin_id": checkin_id, "reason": reason,
            "correction_batch_id": batch_id,
        }
        self.ledger.append("check_in_reversed", payload)
        return payload

    # ---------------------------------------------------------------- 转让
    def transfer_package(
        self, sale_id: str, new_member_id: str, to_store_id: str | None = None
    ) -> dict[str, Any]:
        st = self.state()
        sale = st.sales.get(sale_id)
        if not sale:
            raise NotFoundError(f"销售单不存在: {sale_id}")
        require(new_member_id, "缺少 new_member_id")
        if to_store_id and to_store_id not in st.stores:
            raise NotFoundError(f"目标门店不存在: {to_store_id}")
        payload = {
            "sale_id": sale_id, "new_member_id": new_member_id,
            "from_store_id": sale.service_store_id,
            "to_store_id": to_store_id,
        }
        self.ledger.append("package_transferred", payload)
        return payload

    # -------------------------------------------------------- 退款 / 坏账
    def _write_off(
        self, event_type: str, data: dict[str, Any], id_prefix: str
    ) -> dict[str, Any]:
        st = self.state()
        sale_id = require(data.get("sale_id"), "缺少 sale_id")
        sale = st.sales.get(sale_id)
        if not sale:
            raise NotFoundError(f"销售单不存在: {sale_id}")
        amount = cents(data.get("amount_cents"), "amount_cents")
        reason = require(data.get("reason"), "必须填写原因")
        date = parse_date(data.get("date") or "")
        already = sale.refunded_cents + sale.bad_debt_cents
        if already + amount > sale.price_cents:
            raise ConflictError(
                f"累计退款/坏账不得超过课包金额 {sale.price_cents} 分"
            )
        if event_type == "bad_debt_written_off" and not sale.on_credit:
            raise ConflictError("只有赊销课包可以记坏账")
        batch_id = None
        if period_of(date) in st.locked_periods:
            # 锁定期间记退款/坏账：理由必填，并挂到开启中的更正批次。
            batch_id = self._require_open_batch(
                st, period_of(date), data.get("correction_batch_id")
            )
        obj_id = data.get("id") or new_id(id_prefix)
        payload = {
            "id": obj_id, "sale_id": sale_id, "amount_cents": amount,
            "reason": reason, "date": date, "correction_batch_id": batch_id,
        }
        self.ledger.append(event_type, payload)
        return payload

    def refund(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._write_off("refund_recorded", data, "ref")

    def bad_debt(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._write_off("bad_debt_written_off", data, "bdt")

    # ------------------------------------------------------------- 月结 / 更正
    def lock_settlement(self, period: str, currency: str = "CNY") -> dict[str, Any]:
        """锁定某月结算：期间编号确定性派生，重跑结果必须一致。"""
        from domain import parse_period
        parse_period(period)
        st = self.state()
        if period in st.locked_periods:
            raise ConflictError(f"期间 {period} 已锁定，不能重复锁定")
        batch_id = f"STL-{period}-{currency}"
        payload = {
            "batch_id": batch_id, "period": period, "currency": currency,
            "locked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "head_seq": self.ledger.head_seq(),
        }
        self.ledger.append("settlement_locked", payload)
        return payload

    def open_correction(self, period: str, reason: str) -> dict[str, Any]:
        from domain import parse_period
        parse_period(period)
        require(reason, "更正批次必须填写理由")
        st = self.state()
        if period not in st.locked_periods:
            raise ConflictError(f"期间 {period} 尚未锁定，无需更正批次")
        stl = st.locked_periods[period]
        # 编号 = 原结算编号 + 更正序号，确定性、可重跑。
        seq = 1 + sum(
            1 for b in st.correction_batches.values() if b["period"] == period
        )
        batch_id = f"COR-{period}-{stl['currency']}-{seq:02d}"
        payload = {
            "correction_batch_id": batch_id, "period": period, "reason": reason,
            "opened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.ledger.append("correction_opened", payload)
        return payload

    def post_correction(
        self, batch_id: str, adjustments: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """过账更正批次：补录/冲销/退款已在批次开启期间追加，
        这里只允许再附带带理由的手工调整。过账后批次冻结。"""
        st = self.state()
        batch = st.correction_batches.get(batch_id)
        if not batch:
            raise NotFoundError(f"更正批次不存在: {batch_id}")
        if batch["status"] != "open":
            raise ConflictError(f"更正批次 {batch_id} 已过账")
        clean_adjustments: list[dict[str, Any]] = []
        for adj in adjustments or []:
            target_type = adj.get("target_type")
            if target_type not in ("person", "store"):
                raise DomainError("手工调整 target_type 必须是 person 或 store")
            target_id = require(adj.get("target_id"), "手工调整缺少 target_id")
            if target_type == "person" and target_id not in st.people:
                raise NotFoundError(f"人员不存在: {target_id}")
            if target_type == "store" and target_id not in st.stores:
                raise NotFoundError(f"门店不存在: {target_id}")
            amount = adj.get("amount_cents")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
                raise DomainError("手工调整 amount_cents 必须是非零整数（负为扣减）")
            reason = require(adj.get("reason"), "每条手工调整必须填写理由")
            clean_adjustments.append({
                "target_type": target_type, "target_id": target_id,
                "amount_cents": amount, "reason": reason,
                "kind": adj.get("kind", "manual_adjustment"),
            })
        if request_id and request_id in st.correction_requests:
            raise ConflictError(
                f"请求 {request_id} 已过账为 {st.correction_requests[request_id]}"
            )
        payload = {
            "correction_batch_id": batch_id, "period": batch["period"],
            "reason": batch["reason"], "adjustments": clean_adjustments,
            "request_id": request_id,
            "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.ledger.append("correction_posted", payload)
        return payload
