"""金额与比率计算：内部一律使用整数"分"，比率用 Decimal 解析。"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def to_rate(value: object) -> Decimal:
    """把 0.5 / "0.5" 等输入安全转为 Decimal，并校验在 [0, 1]。"""
    rate = Decimal(str(value))
    if not (Decimal(0) <= rate <= Decimal(1)):
        raise ValueError(f"比率必须在 0 到 1 之间: {value}")
    return rate


def ratio_amount(amount_cents: int, rate: Decimal) -> int:
    """按比率分钱，四舍五入到分（ROUND_HALF_UP）。"""
    if amount_cents < 0:
        return -ratio_amount(-amount_cents, rate)
    return int((Decimal(amount_cents) * rate).quantize(CENT, rounding=ROUND_HALF_UP))


def split_price(price_cents: int, sessions: int) -> list[int]:
    """把课包总价拆成每次核销的确认收入，余数放到最后一次。

    例如 10000 分 / 3 次 -> [3333, 3333, 3334]。
    实际取数按核销顺序：未履约的最后一次自然拿不到余数。
    """
    if sessions <= 0:
        raise ValueError("课次数量必须为正整数")
    base, rem = divmod(price_cents, sessions)
    return [base] * (sessions - 1) + [base + rem]
