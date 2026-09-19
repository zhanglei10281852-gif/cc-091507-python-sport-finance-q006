"""事件溯源投影内核。

把只追加的事件流重放成：

1. 当前业务状态（课包、排课、预约、转介绍、替补关系）；
2. 一组**不可变的分润分录** Entry —— 教练 / 销售 / 门店各一行，带符号；
3. 月结批次（open -> locked -> corrected）。

设计约束（对应运营诉求）：

- 分录金额在生成时按“服务发生时生效的规则版本”计算，并把版本号快照在分录上；
- 退款 / 坏账不删历史，只追加 reversal_of 指向原分录的负数反向分录；
  反向分录入账在“退款发生的当期”，已锁定账期保持不动；
- 课包转让只移动未履约负债，已核销收入留在原服务门店；
- 签到必须关联原预约记录；团课提成发给实际上课教练（替补生效时是替补）；
- 拉新奖励每位被介绍人至多一次；
- 已锁定账期不允许普通业务事件写入，只能用带理由的 settlement_correction
  生成更正批次；
- 一切派生标识（entry_id / batch_no / 校验和）只依赖事件内容，重放结果稳定。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from .events import (
    BAD_DEBT,
    CHECKED_IN,
    CLASS_BOOKED,
    MONTH_SETTLED,
    PACKAGE_PURCHASED,
    PACKAGE_TRANSFERRED,
    REFERRAL,
    REFUND,
    RULE_VERSION_PUBLISHED,
    SESSION_SCHEDULED,
    SETTLEMENT_CORRECTION,
    SUBSTITUTE,
    Event,
    parse_time,
    service_period,
)
from .money import quantize_quantity
from .rules import RuleBook, RuleVersion

# 分录来源标签
SRC_REVENUE = "课包核销收入"
SRC_COACH_COMMISSION = "授课提成"
SRC_REFERRAL = "拉新奖励"
SRC_REFUND_REVERSAL = "退款冲回"
SRC_BAD_DEBT_REVERSAL = "坏账冲回"
SRC_CORRECTION = "月结更正"

PAYEE_COACH = "coach"
PAYEE_SALES = "sales"
PAYEE_STORE = "store"


class DomainError(Exception):
    """业务规则冲突。"""


@dataclass(frozen=True)
class Entry:
    entry_id: str
    period: str
    event_id: str
    source: str
    store_id: str
    payee_type: str
    payee_id: str
    amount_cents: int
    currency: str
    rule_version: str
    memo: str = ""
    reversal_of: str | None = None
    batch_no: str | None = None
    linked_record_id: str | None = None  # 关联原记录（预约/分录/课包）

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "period": self.period,
            "event_id": self.event_id,
            "source": self.source,
            "store_id": self.store_id,
            "payee_type": self.payee_type,
            "payee_id": self.payee_id,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "rule_version": self.rule_version,
            "memo": self.memo,
            "reversal_of": self.reversal_of,
            "batch_no": self.batch_no,
            "linked_record_id": self.linked_record_id,
        }


@dataclass
class Batch:
    batch_no: str
    period: str
    state: str  # locked | corrected
    entry_ids: list[str]
    reason: str | None = None
    settled_by_event: str = ""

    def to_dict(self) -> dict:
        return {
            "batch_no": self.batch_no,
            "period": self.period,
            "state": self.state,
            "reason": self.reason,
            "entry_ids": list(self.entry_ids),
            "settled_by_event": self.settled_by_event,
        }


@dataclass
class _Package:
    package_id: str
    member_id: str
    store_id: str
    total_qty: Decimal
    remaining_qty: Decimal
    currency: str
    total_amount_cents: int = 0
    status: str = "active"  # active | bad_debt
    sales_owner_id: str | None = None
    previous_owners: list[str] = field(default_factory=list)  # 课包转让的历任持有人


@dataclass
class _Session:
    session_id: str
    store_id: str
    coach_id: str
    start_time: str
    class_type: str
    actual_coach_id: str | None = None
    substitute_event_id: str | None = None


@dataclass
class _Booking:
    booking_id: str
    session_id: str
    member_id: str
    package_id: str
    qty: Decimal
    status: str = "booked"  # booked | checked_in
    checkin_event_id: str | None = None


@dataclass
class _Referral:
    referral_id: str
    member_id: str
    sales_owner_id: str
    store_id: str
    rewarded: bool = False
    reward_entry_id: str | None = None


@dataclass
class MaterializedView:
    entries: list[Entry] = field(default_factory=list)
    rules: RuleBook = field(default_factory=RuleBook)
    settlements: dict[str, list[Batch]] = field(default_factory=dict)
    packages: dict[str, _Package] = field(default_factory=dict)
    sessions: dict[str, _Session] = field(default_factory=dict)
    bookings: dict[str, _Booking] = field(default_factory=dict)
    referrals: dict[str, _Referral] = field(default_factory=dict)
    member_referral: dict[str, str] = field(default_factory=dict)
    coaches: dict[str, str] = field(default_factory=dict)  # coach_id -> 最近所属门店
    # checkin（或补录核销）对应预约 -> 该次核销产生的全部分录 id
    # key 为 booking_id：每个预约至多核销一次，退款/坏账按预约精确冲回
    checkin_chains: dict[str, list[str]] = field(default_factory=dict)

    # ----- 查询辅助 -----

    def entries_by_person(self, payee_id: str) -> list[Entry]:
        return [
            e
            for e in self.entries
            if e.payee_type in (PAYEE_COACH, PAYEE_SALES) and e.payee_id == payee_id
        ]

    def entries_by_store(self, store_id: str) -> list[Entry]:
        return [e for e in self.entries if e.store_id == store_id]

    def period_state(self, period: str) -> str:
        if period not in self.settlements:
            return "open"
        return "corrected" if len(self.settlements[period]) > 1 else "locked"

    def batch(self, batch_no: str) -> Batch | None:
        for batches in self.settlements.values():
            for b in batches:
                if b.batch_no == batch_no:
                    return b
        return None

    def entries_of_batch(self, batch_no: str) -> list[Entry]:
        batch = self.batch(batch_no)
        if batch is None:
            return []
        wanted = set(batch.entry_ids)
        return [e for e in self.entries if e.entry_id in wanted]


def _commission_cents(revenue_cents: int, bps: int) -> int:
    """按基点（万分之一）算提成，正数四舍五入，负数（反向）按绝对值取反。"""
    sign = -1 if revenue_cents < 0 else 1
    value = abs(revenue_cents) * bps
    return sign * ((value + 5000) // 10000)


def _canonical_entries(entries: list[Entry]) -> str:
    # batch_no 是结算时回填的派生字段，不参与校验和；
    # 这样“锁定后重放（分录已带批次号）”与“锁定当时（批次号为空）”算出的和一致。
    rows = []
    for e in entries:
        d = e.to_dict()
        d["batch_no"] = None
        rows.append(d)
    return json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _batch_checksum(entries: list[Entry]) -> str:
    digest = hashlib.sha256(_canonical_entries(entries).encode("utf-8")).hexdigest()
    return digest[:8].upper()


class _Projector:
    def __init__(self) -> None:
        self.view = MaterializedView()
        self._seq: dict[str, int] = {}  # 每个事件内的分录序号
        self._reversed_entries: set[str] = set()  # 已冲回的原分录，禁止重复冲回

    # ----- 工具 -----

    def _require_unlocked(self, period: str) -> None:
        if period in self.view.settlements:
            raise DomainError(
                f"账期 {period} 已月结锁定，普通业务不得追写；"
                "请使用带理由的更正批次"
            )

    def _add_entry(
        self,
        event: Event,
        *,
        source: str,
        store_id: str,
        payee_type: str,
        payee_id: str,
        amount_cents: int,
        currency: str,
        rule_version: str,
        memo: str = "",
        reversal_of: str | None = None,
        period: str | None = None,
        linked_record_id: str | None = None,
    ) -> Entry:
        seq = self._seq.get(event.event_id, 0)
        self._seq[event.event_id] = seq + 1
        entry_id = f"{event.event_id}:L{seq:02d}:{payee_type}:{payee_id}"
        entry = Entry(
            entry_id=entry_id,
            period=period or service_period(event.service_time),
            event_id=event.event_id,
            source=source,
            store_id=store_id,
            payee_type=payee_type,
            payee_id=payee_id,
            amount_cents=amount_cents,
            currency=currency,
            rule_version=rule_version,
            memo=memo,
            reversal_of=reversal_of,
            linked_record_id=linked_record_id,
        )
        self.view.entries.append(entry)
        return entry

    def _find_entry(self, entry_id: str) -> Entry:
        for e in self.view.entries:
            if e.entry_id == entry_id:
                return e
        raise DomainError(f"待冲回分录不存在: {entry_id}")

    def _reverse(self, event: Event, original: Entry, source: str, memo: str) -> Entry:
        """在退款/坏账发生的当期追加一笔与原分录金额相反的分录。"""
        return self._add_entry(
            event,
            source=source,
            store_id=original.store_id,
            payee_type=original.payee_type,
            payee_id=original.payee_id,
            amount_cents=-original.amount_cents,
            currency=original.currency,
            rule_version=original.rule_version,
            memo=memo,
            reversal_of=original.entry_id,
            period=service_period(event.service_time),
            linked_record_id=original.linked_record_id,
        )

    def _serving_coach(self, session: _Session) -> str:
        return session.actual_coach_id or session.coach_id

    # ----- 各类事件 -----

    def _on_rule_published(self, event: Event) -> None:
        rule = RuleVersion.from_payload(event.payload)
        try:
            self.view.rules.publish(rule)
        except ValueError as exc:
            raise DomainError(str(exc)) from exc

    def _on_package_purchased(self, event: Event) -> None:
        p = event.payload
        package = _Package(
            package_id=p["package_id"],
            member_id=p["member_id"],
            store_id=p["store_id"],
            total_qty=quantize_quantity(p.get("total_qty", 0)),
            remaining_qty=quantize_quantity(p.get("total_qty", 0)),
            currency=p.get("currency", "CNY"),
            total_amount_cents=int(p.get("total_amount_cents", 0)),
            sales_owner_id=p.get("sales_owner_id"),
        )
        if package.total_qty <= 0:
            raise DomainError("课包课节数必须为正")
        if package.package_id in self.view.packages:
            raise DomainError(f"课包已存在: {package.package_id}")
        self.view.packages[package.package_id] = package

    def _on_session_scheduled(self, event: Event) -> None:
        p = event.payload
        session = _Session(
            session_id=p["session_id"],
            store_id=p["store_id"],
            coach_id=p["coach_id"],
            start_time=p.get("start_time", event.service_time),
            class_type=p.get("class_type", "group"),
        )
        if session.session_id in self.view.sessions:
            raise DomainError(f"排课已存在: {session.session_id}")
        self.view.sessions[session.session_id] = session
        self.view.coaches.setdefault(session.coach_id, session.store_id)

    def _on_substitute(self, event: Event) -> None:
        p = event.payload
        session = self.view.sessions.get(p["session_id"])
        if session is None:
            raise DomainError(f"替补指向的排课不存在: {p['session_id']}")
        planned = session.actual_coach_id or session.coach_id
        if p.get("original_coach_id") and p["original_coach_id"] != planned:
            raise DomainError("替补事件中的原教练与排课不符")
        if not p.get("reason"):
            raise DomainError("教练请假/替补必须填写原因")
        if parse_time(event.service_time) > parse_time(session.start_time):
            raise DomainError("替补登记必须在课程开始之前")
        session.actual_coach_id = p["substitute_coach_id"]
        session.substitute_event_id = event.event_id
        self.view.coaches.setdefault(p["substitute_coach_id"], session.store_id)

    def _on_class_booked(self, event: Event) -> None:
        p = event.payload
        session = self.view.sessions.get(p["session_id"])
        if session is None:
            raise DomainError(f"预约指向的排课不存在: {p['session_id']}")
        package = self.view.packages.get(p["package_id"])
        if package is None:
            raise DomainError(f"预约指向的课包不存在: {p['package_id']}")
        if package.member_id != p["member_id"]:
            raise DomainError("课包不属于该会员，无法预约")
        if package.status != "active":
            raise DomainError("课包已坏账，不能继续预约")
        if service_period(session.start_time) in self.view.settlements:
            raise DomainError("排课所属账期已锁定，不能新增预约")
        qty = quantize_quantity(p.get("qty", 1))
        if qty <= 0:
            raise DomainError("预约课节数必须为正")
        if package.remaining_qty < qty:
            raise DomainError("课包剩余课节不足")
        booking = _Booking(
            booking_id=p["booking_id"],
            session_id=session.session_id,
            member_id=p["member_id"],
            package_id=package.package_id,
            qty=qty,
        )
        if booking.booking_id in self.view.bookings:
            raise DomainError(f"预约已存在: {booking.booking_id}")
        self.view.bookings[booking.booking_id] = booking
        package.remaining_qty -= qty

    def _active_rule(self, service_time: str) -> RuleVersion:
        try:
            return self.view.rules.active_at(service_time)
        except LookupError as exc:
            raise DomainError(str(exc)) from exc

    def _resolve_unit_price(self, p: dict, package: _Package) -> int:
        if p.get("unit_amount_cents") is not None:
            unit_price = int(p["unit_amount_cents"])
        elif p.get("package_total_amount_cents") is not None:
            total = int(p["package_total_amount_cents"])
            unit_price = int(
                (Decimal(total) / package.total_qty).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
        elif package.total_amount_cents > 0:
            unit_price = int(
                (
                    Decimal(package.total_amount_cents) / package.total_qty
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
        else:
            raise DomainError(
                "签到缺少核销单价：请提供 unit_amount_cents，或在购课时登记总金额"
            )
        if unit_price <= 0:
            raise DomainError("核销单价必须为正")
        return unit_price

    def _consume_booking(
        self,
        event: Event,
        booking: _Booking,
        rule: RuleVersion,
        unit_price: int,
        period: str,
        memo_prefix: str = "",
    ) -> list[Entry]:
        """核销一次预约，生成分录并更新预约状态。开放期签到与锁定期补录共用。"""
        session = self.view.sessions[booking.session_id]
        package = self.view.packages[booking.package_id]
        revenue_cents = unit_price * int(booking.qty)

        serving_coach = self._serving_coach(session)
        substitute_note = ""
        if session.actual_coach_id and session.actual_coach_id != session.coach_id:
            substitute_note = (
                f"；原教练 {session.coach_id} 请假，由 {serving_coach} 替补"
                f"（替补记录 {session.substitute_event_id}）"
            )
        memo_session = (
            f"团课 {session.class_type} / 排课 {session.session_id}{substitute_note}"
        )

        booking.status = "checked_in"
        booking.checkin_event_id = event.event_id

        created: list[Entry] = []
        rev_entry = self._add_entry(
            event,
            source=SRC_REVENUE,
            store_id=session.store_id,
            payee_type=PAYEE_STORE,
            payee_id=session.store_id,
            amount_cents=revenue_cents,
            currency=package.currency,
            rule_version=rule.version_id,
            memo=f"核销课包 {package.package_id}，{memo_session}",
            period=period,
            linked_record_id=booking.booking_id,
        )
        created.append(rev_entry)
        commission = _commission_cents(revenue_cents, rule.coach_bps)
        coach_entry = self._add_entry(
            event,
            source=SRC_COACH_COMMISSION,
            store_id=session.store_id,
            payee_type=PAYEE_COACH,
            payee_id=serving_coach,
            amount_cents=commission,
            currency=package.currency,
            rule_version=rule.version_id,
            memo=memo_session.lstrip("；"),
            period=period,
            linked_record_id=booking.booking_id,
        )
        created.append(coach_entry)
        # 暂存核销链路（按预约），供退款/坏账精确冲回
        self.view.checkin_chains.setdefault(booking.booking_id, []).extend(
            [rev_entry.entry_id, coach_entry.entry_id]
        )

        # 拉新奖励：被介绍会员首次签到且规则要求签到后兑现，仅一次
        referral_id = self.view.member_referral.get(booking.member_id)
        if referral_id and rule.referral_on_checkin:
            referral = self.view.referrals[referral_id]
            if not referral.rewarded:
                entry = self._add_entry(
                    event,
                    source=SRC_REFERRAL,
                    store_id=session.store_id,
                    payee_type=PAYEE_SALES,
                    payee_id=referral.sales_owner_id,
                    amount_cents=rule.referral_reward_cents,
                    currency=package.currency,
                    rule_version=rule.version_id,
                    memo=(
                        f"新会员 {booking.member_id} 首次签到核销，"
                        f"转介绍 {referral_id}"
                    ),
                    period=period,
                    linked_record_id=referral_id,
                )
                referral.rewarded = True
                referral.reward_entry_id = entry.entry_id
                created.append(entry)
                self.view.checkin_chains[booking.booking_id].append(entry.entry_id)
        return created

    def _on_checked_in(self, event: Event) -> None:
        p = event.payload
        booking_id = p.get("booking_id")
        # 签到（含补录）必须关联原预约记录
        if not booking_id:
            raise DomainError("签到必须关联原预约记录 booking_id")
        booking = self.view.bookings.get(booking_id)
        if booking is None:
            raise DomainError(f"签到关联的原预约记录不存在: {booking_id}")
        if booking.status == "checked_in":
            raise DomainError(f"预约已签到，禁止重复签到: {booking_id}")
        session = self.view.sessions[booking.session_id]
        package = self.view.packages[booking.package_id]
        period = service_period(session.start_time)
        self._require_unlocked(period)
        rule = self._active_rule(session.start_time)
        unit_price = self._resolve_unit_price(p, package)
        self._consume_booking(event, booking, rule, unit_price, period)

    def _on_referral(self, event: Event) -> None:
        p = event.payload
        referral_id = p["referral_id"]
        if referral_id in self.view.referrals:
            raise DomainError(f"转介绍记录已存在: {referral_id}")
        member_id = p["referred_member_id"]
        if member_id in self.view.member_referral:
            raise DomainError(f"会员已存在转介绍关系，禁止重复计提: {member_id}")
        referral = _Referral(
            referral_id=referral_id,
            member_id=member_id,
            sales_owner_id=p["sales_owner_id"],
            store_id=p["store_id"],
        )
        self.view.referrals[referral_id] = referral
        self.view.member_referral[member_id] = referral_id

        # 旧版规则可在转介绍成立时即时计提；签到后计提的奖励在首次核销时生成
        rule = self._active_rule(event.service_time)
        if not rule.referral_on_checkin and rule.referral_reward_cents > 0:
            period = service_period(event.service_time)
            self._require_unlocked(period)
            entry = self._add_entry(
                event,
                source=SRC_REFERRAL,
                store_id=p["store_id"],
                payee_type=PAYEE_SALES,
                payee_id=p["sales_owner_id"],
                amount_cents=rule.referral_reward_cents,
                currency=p.get("currency", "CNY"),
                rule_version=rule.version_id,
                memo=f"转介绍即时计提 {referral_id}（新会员 {member_id}）",
                period=period,
                linked_record_id=referral_id,
            )
            referral.rewarded = True
            referral.reward_entry_id = entry.entry_id

    def _chain_entries(self, checkin_event_id: str) -> list[Entry]:
        booking_id = next(
            (
                b.booking_id
                for b in self.view.bookings.values()
                if b.checkin_event_id == checkin_event_id
            ),
            None,
        )
        if booking_id is None:
            raise DomainError(f"待冲回的签到核销记录不存在: {checkin_event_id}")
        ids = self.view.checkin_chains.get(booking_id)
        if ids is None:
            raise DomainError(f"待冲回的签到核销记录不存在: {checkin_event_id}")
        return [self._find_entry(i) for i in ids]

    def _reverse_checked_in_entry(
        self, event: Event, original: Entry, reason: str, source: str
    ) -> None:
        if original.entry_id in self._reversed_entries:
            raise DomainError(
                f"核销分录 {original.entry_id} 已被冲回，禁止重复冲回"
            )
        label = {
            SRC_REVENUE: "核销收入",
            SRC_COACH_COMMISSION: "授课提成",
            SRC_REFERRAL: "拉新奖励",
        }.get(original.source, original.source)
        self._reverse(
            event,
            original,
            source,
            f"{'退款' if source == SRC_REFUND_REVERSAL else '坏账'}冲回{label}"
            f"（原分录 {original.entry_id}，原因：{reason}）",
        )
        self._reversed_entries.add(original.entry_id)

    def _member_has_valid_consumption(self, member_id: str) -> bool:
        """该会员是否仍存在未被冲回的有效核销（收入分录未反向）。"""
        for booking in self.view.bookings.values():
            if booking.member_id != member_id or not booking.checkin_event_id:
                continue
            for entry_id in self.view.checkin_chains.get(booking.booking_id, []):
                entry = self._find_entry(entry_id)
                if entry.source != SRC_REVENUE:
                    continue
                if entry.entry_id not in self._reversed_entries:
                    return True
        return False

    def _on_refund(self, event: Event) -> None:
        p = event.payload
        package = self.view.packages.get(p["package_id"])
        if package is None:
            raise DomainError(f"退款指向的课包不存在: {p['package_id']}")
        if not p.get("reason"):
            raise DomainError("退款必须填写原因")
        refund_period = service_period(event.service_time)
        self._require_unlocked(refund_period)
        rule = self._active_rule(event.service_time)
        checkin_ids = p.get("reverse_checkin_ids", [])

        affected_members: set[str] = set()
        referral_entries: list[Entry] = []
        for checkin_id in checkin_ids:
            chain = self._chain_entries(checkin_id)
            booking_id = next(
                (
                    b.booking_id
                    for b in self.view.bookings.values()
                    if b.checkin_event_id == checkin_id
                ),
                None,
            )
            booking = self.view.bookings[booking_id] if booking_id else None
            if booking and booking.package_id != package.package_id:
                raise DomainError(f"退款课包与核销记录 {checkin_id} 不符")
            if booking:
                affected_members.add(booking.member_id)
            for original in chain:
                if original.source == SRC_REVENUE:
                    # 已核销课节退款：收入始终退回
                    self._reverse_checked_in_entry(
                        event, original, p["reason"], SRC_REFUND_REVERSAL
                    )
                elif original.source == SRC_COACH_COMMISSION:
                    # 是否追回已核销课节的授课提成，按退款时生效的规则执行
                    if rule.clawback_consumed_refund:
                        self._reverse_checked_in_entry(
                            event, original, p["reason"], SRC_REFUND_REVERSAL
                        )
                elif original.source == SRC_REFERRAL:
                    referral_entries.append(original)

        # 拉新奖励仅在“会员已无任何有效核销”（拉新成立条件整体失效）时追回，
        # 单次课退款不影响奖励，避免同一笔奖励被反复争执。
        for member_id in affected_members:
            if self._member_has_valid_consumption(member_id):
                continue
            referral_id = self.view.member_referral.get(member_id)
            if not referral_id:
                continue
            referral = self.view.referrals[referral_id]
            candidate_ids = [
                e.entry_id
                for e in referral_entries
                if e.payee_id == referral.sales_owner_id
            ]
            if referral.reward_entry_id:
                candidate_ids.append(referral.reward_entry_id)
            for entry_id in dict.fromkeys(candidate_ids):
                original = self._find_entry(entry_id)
                if original.entry_id in self._reversed_entries:
                    continue
                self._reverse_checked_in_entry(
                    event, original, p["reason"], SRC_REFUND_REVERSAL
                )
        # 纯未核销课节退款（reverse_checkin_ids 为空）：只动负债，不产生损益分录

    def _on_bad_debt(self, event: Event) -> None:
        p = event.payload
        package = self.view.packages.get(p["package_id"])
        if package is None:
            raise DomainError(f"坏账指向的课包不存在: {p['package_id']}")
        if package.status == "bad_debt":
            raise DomainError(f"课包已记坏账: {package.package_id}")
        period = service_period(event.service_time)
        self._require_unlocked(period)
        if not p.get("reason"):
            raise DomainError("坏账必须填写原因")
        rule = self._active_rule(event.service_time)

        # 冲回该课包所有尚未冲回的核销链路（收入 + 提成；按规则决定是否含拉新奖励）
        for booking in self.view.bookings.values():
            if booking.package_id != package.package_id or not booking.checkin_event_id:
                continue
            for original in self._chain_entries(booking.checkin_event_id):
                if (
                    original.source == SRC_REFERRAL
                    and not rule.referral_clawback_on_bad_debt
                ):
                    continue
                if original.entry_id in self._reversed_entries:
                    continue
                self._reverse_checked_in_entry(
                    event, original, p["reason"], SRC_BAD_DEBT_REVERSAL
                )

        # 即时计提的拉新奖励（旧版规则）不挂在核销链路上，坏账时按规则单独追回。
        # 课包可能已转让，需要同时考虑历任持有人。
        if rule.referral_clawback_on_bad_debt:
            candidate_members = {package.member_id, *package.previous_owners}
            for member_id in candidate_members:
                referral_id = self.view.member_referral.get(member_id)
                if not referral_id:
                    continue
                referral = self.view.referrals[referral_id]
                if (
                    referral.reward_entry_id
                    and referral.reward_entry_id not in self._reversed_entries
                ):
                    original = self._find_entry(referral.reward_entry_id)
                    self._reverse_checked_in_entry(
                        event, original, p["reason"], SRC_BAD_DEBT_REVERSAL
                    )
        package.status = "bad_debt"

    def _on_package_transferred(self, event: Event) -> None:
        p = event.payload
        package = self.view.packages.get(p["package_id"])
        if package is None:
            raise DomainError(f"转让指向的课包不存在: {p['package_id']}")
        if package.remaining_qty <= 0:
            raise DomainError("课包已无剩余课节，不能转让")
        if p["to_member_id"] == package.member_id:
            raise DomainError("课包受让会员必须与当前持有人不同")
        old_owner = package.member_id
        package.previous_owners.append(old_owner)
        package.member_id = p["to_member_id"]
        if p.get("to_store_id"):
            package.store_id = p["to_store_id"]
        package.sales_owner_id = p.get("sales_owner_id", package.sales_owner_id)
        # 转介绍关系不随课包转移：受让方不产生新的拉新奖励，避免重复计提。
        # 历史核销分录一律不动 —— 收入责任留在真实服务发生处（原门店）；
        # 转让后的新核销才按新门店/新归属记账。

    def _on_month_settled(self, event: Event) -> None:
        period = event.payload["period"]
        if period in self.view.settlements:
            raise DomainError(f"账期已结算: {period}")
        pending = [
            e
            for e in self.view.entries
            if e.period == period and e.batch_no is None
        ]
        pending.sort(key=lambda e: e.entry_id)
        checksum = _batch_checksum(pending)
        batch_no = f"JS{period.replace('-', '')}-{checksum}"
        for e in pending:
            # frozen dataclass：替换 batch_no 字段
            object.__setattr__(e, "batch_no", batch_no)
        batch = Batch(
            batch_no=batch_no,
            period=period,
            state="locked",
            entry_ids=[e.entry_id for e in pending],
            settled_by_event=event.event_id,
        )
        self.view.settlements[period] = [batch]

    def _backfill_booking(self, item: dict) -> _Booking:
        """锁定期补建预约记录（纸质单事后录入），校验同普通预约但允许账期已锁。"""
        session = self.view.sessions.get(item["session_id"])
        if session is None:
            raise DomainError(f"补录预约指向的排课不存在: {item['session_id']}")
        package = self.view.packages.get(item["package_id"])
        if package is None:
            raise DomainError(f"补录预约指向的课包不存在: {item['package_id']}")
        if package.member_id != item["member_id"]:
            raise DomainError("课包不属于该会员，无法补录预约")
        if package.status != "active":
            raise DomainError("课包已坏账，不能补录预约")
        qty = quantize_quantity(item.get("qty", 1))
        if qty <= 0:
            raise DomainError("预约课节数必须为正")
        if package.remaining_qty < qty:
            raise DomainError("课包剩余课节不足")
        booking = _Booking(
            booking_id=item["booking_id"],
            session_id=session.session_id,
            member_id=item["member_id"],
            package_id=package.package_id,
            qty=qty,
        )
        if booking.booking_id in self.view.bookings:
            raise DomainError(f"预约已存在: {booking.booking_id}")
        self.view.bookings[booking.booking_id] = booking
        package.remaining_qty -= qty
        return booking

    def _on_settlement_correction(self, event: Event) -> None:
        p = event.payload
        period = p["period"]
        reason = p.get("reason", "").strip()
        if not reason:
            raise DomainError("更正批次必须附带理由")
        if period not in self.view.settlements:
            raise DomainError(f"只能对已锁定账期更正: {period} 尚未月结")
        lines = p.get("lines", [])
        late_checkins = p.get("late_checkins", [])
        late_bookings = p.get("late_bookings", [])
        if not lines and not late_checkins and not late_bookings:
            raise DomainError("更正批次至少包含一条调整或一笔补录核销")

        batches = self.view.settlements[period]
        for b in batches:
            object.__setattr__(b, "state", "corrected")
        seq = len(batches)  # 首个更正批次 -> C1
        base = batches[0].batch_no.rsplit("-", 1)[0]
        batch_no = f"{base}-C{seq}"

        new_entries: list[Entry] = []

        # 1) 补建原预约（事后录入的纸质预约）
        for item in late_bookings:
            booking = self._backfill_booking(item)
            session = self.view.sessions[booking.session_id]
            if service_period(session.start_time) != period:
                raise DomainError(
                    f"补录预约 {booking.booking_id} 的上课账期与更正账期 {period} 不符"
                )
            package = self.view.packages[booking.package_id]
            rule = self._active_rule(session.start_time)
            unit_price = self._resolve_unit_price(item, package)
            created = self._consume_booking(
                event,
                booking,
                rule,
                unit_price,
                period,
                memo_prefix=f"【月结后补录·{reason}】",
            )
            new_entries.extend(created)

        # 2) 已存在预约的补录签到（必须关联原预约记录）
        for item in late_checkins:
            booking_id = item.get("booking_id")
            if not booking_id:
                raise DomainError("补录签到必须关联原预约记录 booking_id")
            booking = self.view.bookings.get(booking_id)
            if booking is None:
                raise DomainError(f"补录签到关联的原预约记录不存在: {booking_id}")
            if booking.status == "checked_in":
                raise DomainError(f"预约已签到，禁止重复补录: {booking_id}")
            session = self.view.sessions[booking.session_id]
            if service_period(session.start_time) != period:
                raise DomainError(
                    f"补录签到 {booking_id} 的上课账期与更正账期 {period} 不符"
                )
            package = self.view.packages[booking.package_id]
            rule = self._active_rule(session.start_time)
            unit_price = self._resolve_unit_price(item, package)
            created = self._consume_booking(
                event,
                booking,
                rule,
                unit_price,
                period,
                memo_prefix=f"【月结后补录·{reason}】",
            )
            new_entries.extend(created)

        # 3) 手工调整分录
        for line in lines:
            amount = int(line["amount_cents"])
            if amount == 0:
                raise DomainError("更正分录金额不能为 0")
            payee_type = line["payee_type"]
            if payee_type not in (PAYEE_COACH, PAYEE_SALES, PAYEE_STORE):
                raise DomainError(f"未知的分润对象类型: {payee_type}")
            if not line.get("store_id"):
                # 门店报表按 store_id 归口，任何调整都必须指明归属门店
                raise DomainError("更正分录必须指定 store_id")
            if line.get("reversal_of"):
                # 反向更正必须指向账期内真实存在的历史分录
                target = next(
                    (e for e in self.view.entries
                     if e.entry_id == line["reversal_of"]),
                    None,
                )
                if target is None:
                    raise DomainError(
                        f"更正分录引用的历史分录不存在: {line['reversal_of']}"
                    )
                if amount > 0:
                    raise DomainError("反向更正金额应为负数")
            entry = self._add_entry(
                event,
                source=SRC_CORRECTION,
                store_id=line.get("store_id", ""),
                payee_type=payee_type,
                payee_id=line["payee_id"],
                amount_cents=amount,
                currency=line.get("currency", "CNY"),
                rule_version=line.get("rule_version", ""),
                memo=f"账期 {period} 更正：{reason}｜{line.get('memo', '')}",
                reversal_of=line.get("reversal_of"),
                period=period,
                linked_record_id=line.get("linked_record_id"),
            )
            new_entries.append(entry)

        # 分录顺序必须确定：统一按 entry_id 排序后入批次
        new_entries.sort(key=lambda e: e.entry_id)
        for e in new_entries:
            object.__setattr__(e, "batch_no", batch_no)
        batch = Batch(
            batch_no=batch_no,
            period=period,
            state="corrected",
            entry_ids=[e.entry_id for e in new_entries],
            reason=reason,
            settled_by_event=event.event_id,
        )
        batches.append(batch)


_HANDLERS = {
    RULE_VERSION_PUBLISHED: _Projector._on_rule_published,
    PACKAGE_PURCHASED: _Projector._on_package_purchased,
    SESSION_SCHEDULED: _Projector._on_session_scheduled,
    SUBSTITUTE: _Projector._on_substitute,
    CLASS_BOOKED: _Projector._on_class_booked,
    CHECKED_IN: _Projector._on_checked_in,
    REFERRAL: _Projector._on_referral,
    REFUND: _Projector._on_refund,
    BAD_DEBT: _Projector._on_bad_debt,
    PACKAGE_TRANSFERRED: _Projector._on_package_transferred,
    MONTH_SETTLED: _Projector._on_month_settled,
    SETTLEMENT_CORRECTION: _Projector._on_settlement_correction,
}


def materialize(events: list[Event]) -> MaterializedView:
    """按追加顺序确定性重放事件流。"""
    projector = _Projector()
    for event in events:
        handler = _HANDLERS.get(event.event_type)
        if handler is None:
            raise DomainError(f"未知事件类型: {event.event_type}")
        handler(projector, event)
    return projector.view
