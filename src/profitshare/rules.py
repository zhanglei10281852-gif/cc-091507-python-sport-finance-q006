"""按生效日切换的提成规则版本。

同一笔收入始终按“服务发生时生效中”的规则计算，并在分录上快照规则版本号；
旧账期重跑不会因为后来发布的新规则而改变结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .events import parse_time


@dataclass(frozen=True)
class RuleVersion:
    version_id: str
    effective_from: str  # ISO 日期，含当天
    coach_bps: int = 0                       # 授课提成：确认收入的基点（1/10000）
    referral_reward_cents: int = 0           # 拉新奖励（分）
    referral_on_checkin: bool = True         # 被介绍人首次签到后奖励才成立
    clawback_consumed_refund: bool = True    # 已核销课节退款是否追回提成
    referral_clawback_on_bad_debt: bool = True  # 坏账时是否追回拉新奖励

    def __post_init__(self) -> None:
        date.fromisoformat(self.effective_from)
        if not 0 <= self.coach_bps <= 10000:
            raise ValueError("coach_bps 必须在 0..10000 之间")
        if self.referral_reward_cents < 0:
            raise ValueError("referral_reward_cents 不能为负")

    def to_payload(self) -> dict:
        return {
            "version_id": self.version_id,
            "effective_from": self.effective_from,
            "coach_bps": self.coach_bps,
            "referral_reward_cents": self.referral_reward_cents,
            "referral_on_checkin": self.referral_on_checkin,
            "clawback_consumed_refund": self.clawback_consumed_refund,
            "referral_clawback_on_bad_debt": self.referral_clawback_on_bad_debt,
        }

    @classmethod
    def from_payload(cls, data: dict) -> "RuleVersion":
        return cls(
            version_id=data["version_id"],
            effective_from=data["effective_from"],
            coach_bps=int(data.get("coach_bps", 0)),
            referral_reward_cents=int(data.get("referral_reward_cents", 0)),
            referral_on_checkin=bool(data.get("referral_on_checkin", True)),
            clawback_consumed_refund=bool(
                data.get("clawback_consumed_refund", True)
            ),
            referral_clawback_on_bad_debt=bool(
                data.get("referral_clawback_on_bad_debt", True)
            ),
        )


class RuleBook:
    """已发布规则版本集合，按服务时间取生效版本。"""

    def __init__(self, versions: list[RuleVersion] | None = None) -> None:
        self._versions: dict[str, RuleVersion] = {}
        for rv in versions or []:
            self.publish(rv)

    def publish(self, rule: RuleVersion) -> None:
        if rule.version_id in self._versions:
            raise ValueError(f"规则版本已存在: {rule.version_id}")
        self._versions[rule.version_id] = rule

    def active_at(self, service_time: str) -> RuleVersion:
        day = parse_time(service_time).date()
        candidates = [
            rv
            for rv in self._versions.values()
            if date.fromisoformat(rv.effective_from) <= day
        ]
        if not candidates:
            raise LookupError(f"{day} 没有生效中的规则版本")
        return max(candidates, key=lambda rv: (rv.effective_from, rv.version_id))

    def get(self, version_id: str) -> RuleVersion:
        return self._versions[version_id]

    def __len__(self) -> int:
        return len(self._versions)
