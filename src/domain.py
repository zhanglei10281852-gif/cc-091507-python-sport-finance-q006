"""领域常量、异常与校验工具。"""
from __future__ import annotations

import re
from typing import Any

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

SETTLEMENT_OPEN = "open"
SETTLEMENT_LOCKED = "locked"
SETTLEMENT_CORRECTED = "corrected"

# 分录类型：settleable=True 的才计入个人/门店"应得金额"。
KIND_COMMISSION = "coach_commission"      # 授课提成（核销时确认）
KIND_REFERRAL = "sales_referral"          # 拉新奖励（成交时一次性确认）
KIND_REVENUE = "store_revenue"            # 门店留存收入（核销时确认）
KIND_DEFERRED = "deferred_revenue"        # 未履约递延（不计应得，仅平衡台账）
KIND_REVERSAL = "reversal"                # 反向分录标记（金额为负）

SOURCE_SALE = "package_sale"
SOURCE_REDEMPTION = "class_redemption"
SOURCE_REVERSAL = "checkin_reversal"
SOURCE_BACKFILL = "attendance_backfill"
SOURCE_REFUND = "refund"
SOURCE_BAD_DEBT = "bad_debt"
SOURCE_ADJUSTMENT = "manual_adjustment"


class DomainError(Exception):
    """请求违反业务规则（400）。"""


class NotFoundError(DomainError):
    """引用对象不存在（404）。"""


class ConflictError(DomainError):
    """状态冲突，如期间已锁定（409）。"""


def require(value: Any, message: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(message)
    return value


def parse_date(value: str) -> str:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise DomainError(f"日期格式应为 YYYY-MM-DD: {value!r}")
    year, month, day = (int(x) for x in value.split("-"))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise DomainError(f"非法日期: {value}")
    return value


def period_of(date_str: str) -> str:
    parse_date(date_str)
    return date_str[:7]


def parse_period(value: str) -> str:
    if not isinstance(value, str) or not PERIOD_RE.match(value):
        raise DomainError(f"结算月份格式应为 YYYY-MM: {value!r}")
    return value


def cents(value: Any, field: str) -> int:
    """金额只接受正整数分，杜绝浮点误差。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError(f"{field} 必须是以分为单位的整数")
    if value <= 0:
        raise DomainError(f"{field} 必须为正数")
    return value
