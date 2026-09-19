"""事件定义与稳定标识。

reference/domain.json 中公开的事件类型：
class_booked / checked_in / referral / refund / substitute /
settlement_correction；另外为本平台的事件溯源需要补充内部事件：
package_purchased / session_scheduled / coach_leave / package_transferred /
bad_debt / rule_version_published / month_settled。

每条事件区分：
- event_id：全局唯一，重放/重试时幂等
- service_time：业务真实发生时间（上课、成交、退款），规则版本与账期都按它归属
- recorded_at：系统接收时间（补录可能显著晚于 service_time）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# 公开事件类型（与 reference/domain.json 保持一致）
CLASS_BOOKED = "class_booked"
CHECKED_IN = "checked_in"
REFERRAL = "referral"
REFUND = "refund"
SUBSTITUTE = "substitute"
SETTLEMENT_CORRECTION = "settlement_correction"

# 内部补充事件类型（请假与替补合并登记为 substitute）
PACKAGE_PURCHASED = "package_purchased"
SESSION_SCHEDULED = "session_scheduled"
PACKAGE_TRANSFERRED = "package_transferred"
BAD_DEBT = "bad_debt"
RULE_VERSION_PUBLISHED = "rule_version_published"
MONTH_SETTLED = "month_settled"

PUBLIC_EVENT_TYPES = frozenset(
    {CLASS_BOOKED, CHECKED_IN, REFERRAL, REFUND, SUBSTITUTE, SETTLEMENT_CORRECTION}
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-:]{0,63}$")


def parse_time(value: str) -> datetime:
    """解析 ISO-8601 时间，允许带 Z 结尾。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def service_period(value: str) -> str:
    """业务发生时间 -> 所属账期 YYYY-MM。"""
    return parse_time(value).strftime("%Y-%m")


def service_day(value: str) -> str:
    return parse_time(value).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    service_time: str
    payload: dict
    recorded_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.event_id):
            raise ValueError(f"非法事件标识: {self.event_id!r}")
        parse_time(self.service_time)
        parse_time(self.recorded_at)
        if not isinstance(self.payload, dict):
            raise ValueError("payload 必须是对象")

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "service_time": self.service_time,
            "recorded_at": self.recorded_at,
            "schema_version": self.schema_version,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            service_time=data["service_time"],
            payload=data["data"] if "data" in data else data["payload"],
            recorded_at=data["recorded_at"],
            schema_version=data.get("schema_version", 1),
        )
