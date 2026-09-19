"""对外服务门面。

职责：
- 把业务动作翻译成事件（默认用业务自然键生成确定性 event_id，重试天然幂等）；
- 写入只追加事件库，并即时重放校验；
- 暴露只读查询（个人台账、门店对比、批次核验）。

财务“重跑结算批次”= 从事件流零重放 materialize()，批次号与内容完全一致；
verify_batch() 用独立重放校验批次校验和。
"""

from __future__ import annotations

import threading
from pathlib import Path

from .engine import DomainError, MaterializedView, materialize
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
)
from .reports import coach_statement, store_comparison
from .repository import EventConflictError, EventStore
from .rules import RuleVersion


class Platform:
    def __init__(self, store: EventStore | str | Path | None = None) -> None:
        self.store = store if isinstance(store, EventStore) else EventStore(store)
        self._lock = threading.RLock()

    # ----- 内部：写事件并重放校验 -----

    def _emit(
        self,
        event_type: str,
        service_time: str,
        payload: dict,
        event_id: str,
        *,
        recorded_at: str | None = None,
    ) -> Event:
        with self._lock:
            # 同 event_id 的命令重试：内容一致则幂等返回，绝不重复入账
            existing = self.store.get(event_id)
            if existing is not None:
                if existing.to_dict() != Event(
                    event_id=event_id,
                    event_type=event_type,
                    service_time=service_time,
                    payload=payload,
                    recorded_at=recorded_at or service_time,
                ).to_dict():
                    raise EventConflictError(f"事件标识冲突且内容不一致: {event_id}")
                return existing
            event = Event(
                event_id=event_id,
                event_type=event_type,
                service_time=service_time,
                payload=payload,
                recorded_at=recorded_at or service_time,
            )
            # 先试投影：校验不过的事件绝不落库
            materialize([*self.store.all(), event])
            self.store.append(event)
            return event

    def rebuild_view(self) -> MaterializedView:
        """从事件流零重放。"""
        return materialize(self.store.all())

    # ----- 规则版本 -----

    def publish_rule(self, rule: RuleVersion) -> Event:
        payload = rule.to_payload()
        return self._emit(
            RULE_VERSION_PUBLISHED,
            service_time=f"{rule.effective_from}T00:00:00",
            payload=payload,
            event_id=f"rule:{rule.version_id}",
        )

    # ----- 销售 / 课包 -----

    def purchase_package(
        self,
        *,
        package_id: str,
        member_id: str,
        store_id: str,
        total_qty,
        total_amount_cents: int,
        currency: str = "CNY",
        sales_owner_id: str | None = None,
        sold_at: str,
        event_id: str | None = None,
    ) -> Event:
        return self._emit(
            PACKAGE_PURCHASED,
            sold_at,
            {
                "package_id": package_id,
                "member_id": member_id,
                "store_id": store_id,
                "total_qty": str(total_qty),
                "total_amount_cents": int(total_amount_cents),
                "currency": currency,
                "sales_owner_id": sales_owner_id,
            },
            event_id or f"pkg:{package_id}",
        )

    # ----- 排课 / 预约 / 签到 -----

    def schedule_session(
        self,
        *,
        session_id: str,
        store_id: str,
        coach_id: str,
        start_time: str,
        class_type: str = "group",
        event_id: str | None = None,
    ) -> Event:
        return self._emit(
            SESSION_SCHEDULED,
            start_time,
            {
                "session_id": session_id,
                "store_id": store_id,
                "coach_id": coach_id,
                "start_time": start_time,
                "class_type": class_type,
            },
            event_id or f"session:{session_id}",
        )

    def register_substitute(
        self,
        *,
        session_id: str,
        original_coach_id: str,
        substitute_coach_id: str,
        reason: str,
        registered_at: str,
        event_id: str | None = None,
    ) -> Event:
        return self._emit(
            SUBSTITUTE,
            registered_at,
            {
                "session_id": session_id,
                "original_coach_id": original_coach_id,
                "substitute_coach_id": substitute_coach_id,
                "reason": reason,
            },
            event_id or f"sub:{session_id}:{substitute_coach_id}",
        )

    def book_class(
        self,
        *,
        booking_id: str,
        session_id: str,
        member_id: str,
        package_id: str,
        qty=1,
        booked_at: str,
        event_id: str | None = None,
    ) -> Event:
        return self._emit(
            CLASS_BOOKED,
            booked_at,
            {
                "booking_id": booking_id,
                "session_id": session_id,
                "member_id": member_id,
                "package_id": package_id,
                "qty": str(qty),
            },
            event_id or f"booking:{booking_id}",
        )

    def check_in(
        self,
        *,
        booking_id: str,
        unit_amount_cents: int | None = None,
        package_total_amount_cents: int | None = None,
        service_time: str | None = None,
        recorded_at: str,
        event_id: str | None = None,
    ) -> Event:
        """签到核销。补录时 service_time 为真实上课时间、recorded_at 为补录时间，
        且必须关联原预约 booking_id。"""
        view = self.rebuild_view()
        booking = view.bookings.get(booking_id)
        if booking is None:
            raise DomainError(f"原预约记录不存在: {booking_id}")
        session = view.sessions[booking.session_id]
        payload: dict = {"booking_id": booking_id}
        if unit_amount_cents is not None:
            payload["unit_amount_cents"] = int(unit_amount_cents)
        elif package_total_amount_cents is not None:
            payload["package_total_amount_cents"] = int(
                package_total_amount_cents
            )
        return self._emit(
            CHECKED_IN,
            service_time or session.start_time,
            payload,
            event_id or f"checkin:{booking_id}",
            recorded_at=recorded_at,
        )

    # ----- 转介绍 -----

    def register_referral(
        self,
        *,
        referral_id: str,
        referred_member_id: str,
        sales_owner_id: str,
        store_id: str,
        occurred_at: str,
        currency: str = "CNY",
        event_id: str | None = None,
    ) -> Event:
        return self._emit(
            REFERRAL,
            occurred_at,
            {
                "referral_id": referral_id,
                "referred_member_id": referred_member_id,
                "sales_owner_id": sales_owner_id,
                "store_id": store_id,
                "currency": currency,
            },
            event_id or f"referral:{referral_id}",
        )

    # ----- 退款 / 坏账 / 转让 -----

    def refund(
        self,
        *,
        refund_id: str,
        package_id: str,
        reason: str,
        reverse_checkin_ids: list[str] | None = None,
        refunded_at: str,
        event_id: str | None = None,
    ) -> Event:
        return self._emit(
            REFUND,
            refunded_at,
            {
                "package_id": package_id,
                "reason": reason,
                "reverse_checkin_ids": list(reverse_checkin_ids or []),
            },
            event_id or f"refund:{refund_id}",
        )

    def bad_debt(
        self,
        *,
        bad_debt_id: str,
        package_id: str,
        reason: str,
        occurred_at: str,
        event_id: str | None = None,
    ) -> Event:
        return self._emit(
            BAD_DEBT,
            occurred_at,
            {"package_id": package_id, "reason": reason},
            event_id or f"baddebt:{bad_debt_id}",
        )

    def transfer_package(
        self,
        *,
        transfer_id: str,
        package_id: str,
        to_member_id: str,
        to_store_id: str | None = None,
        sales_owner_id: str | None = None,
        transferred_at: str,
        event_id: str | None = None,
    ) -> Event:
        return self._emit(
            PACKAGE_TRANSFERRED,
            transferred_at,
            {
                "package_id": package_id,
                "to_member_id": to_member_id,
                "to_store_id": to_store_id,
                "sales_owner_id": sales_owner_id,
            },
            event_id or f"transfer:{transfer_id}",
        )

    # ----- 月结与更正 -----

    def settle_month(self, period: str) -> Event:
        return self._emit(
            MONTH_SETTLED,
            service_time=f"{period}-28T23:59:59",
            payload={"period": period},
            event_id=f"settle:{period}",
        )

    def correct_period(
        self,
        *,
        correction_id: str,
        period: str,
        reason: str,
        lines: list[dict] | None = None,
        late_checkins: list[dict] | None = None,
        late_bookings: list[dict] | None = None,
        created_at: str,
        event_id: str | None = None,
    ) -> Event:
        """对已锁定账期追加带理由的更正批次（不改动历史分录）。

        - lines：手工调整分录（带正负号）；
        - late_checkins：月结后补录签到，必须关联锁定期前已存在的原预约；
        - late_bookings：连原预约都未录入（纸质单），一并补建并核销。
        """
        return self._emit(
            SETTLEMENT_CORRECTION,
            created_at,
            {
                "period": period,
                "reason": reason,
                "lines": list(lines or []),
                "late_checkins": list(late_checkins or []),
                "late_bookings": list(late_bookings or []),
            },
            event_id or f"correction:{period}:{correction_id}",
        )

    # ----- 查询 -----

    def coach_statement(self, person_id: str) -> dict:
        return coach_statement(self.rebuild_view(), person_id)

    def store_comparison(self) -> dict:
        return store_comparison(self.rebuild_view())

    def batch(self, batch_no: str) -> dict | None:
        view = self.rebuild_view()
        batch = view.batch(batch_no)
        if batch is None:
            return None
        return {
            **batch.to_dict(),
            "entries": [e.to_dict() for e in view.entries_of_batch(batch_no)],
            "totals_cents": _totals(view.entries_of_batch(batch_no)),
        }

    def period_batches(self, period: str) -> list[dict]:
        view = self.rebuild_view()
        result = []
        for batch in view.settlements.get(period, []):
            result.append(
                {
                    **batch.to_dict(),
                    "totals_cents": _totals(view.entries_of_batch(batch.batch_no)),
                }
            )
        return result

    def verify_batch(self, batch_no: str) -> dict:
        """从零重放事件流，核对批次编号中的校验和（锁定批次）与批次内容。"""
        from .engine import _batch_checksum

        view = self.rebuild_view()
        batch = view.batch(batch_no)
        if batch is None:
            raise DomainError(f"批次不存在: {batch_no}")
        entries = sorted(view.entries_of_batch(batch_no), key=lambda e: e.entry_id)
        checksum = _batch_checksum(entries)
        locked = view.settlements[batch.period][0]
        is_locked_batch = batch_no == locked.batch_no
        expected_suffix = locked.batch_no.rsplit("-", 1)[-1]
        if is_locked_batch:
            matches = expected_suffix == checksum
        else:
            # 更正批次：重放后批次号必须保持稳定
            matches = batch.batch_no == batch_no
        return {
            "batch_no": batch_no,
            "period": batch.period,
            "state": batch.state,
            "entry_count": len(entries),
            "checksum": checksum,
            "locked_checksum": expected_suffix if is_locked_batch else None,
            "matches": matches,
            "totals_cents": _totals(entries),
        }


def _totals(entries) -> dict:
    """按 (收款方类型, 币种) 汇总；不同币种分开，绝不混加。"""
    totals: dict[str, dict[str, int]] = {}
    for e in entries:
        by_currency = totals.setdefault(e.payee_type, {})
        by_currency[e.currency] = by_currency.get(e.currency, 0) + e.amount_cents
    return totals
