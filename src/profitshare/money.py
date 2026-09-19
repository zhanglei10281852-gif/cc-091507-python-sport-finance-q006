"""金额与数量精度。

金额用整数最小货币单位（分）表示，杜绝浮点误差；课包剩余课节数量
按 reference/domain.json 的 quantity_precision 处理。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

QUANTITY_PRECISION = 6
SUPPORTED_CURRENCIES = ("CNY", "HKD", "USD")
_CENT = Decimal("100")
_Q = Decimal(1).scaleb(-QUANTITY_PRECISION)


def yuan_to_cents(amount: Decimal | int | float | str) -> int:
    """元 -> 分，四舍五入到整数分。"""
    value = Decimal(str(amount))
    if not value.is_finite():
        raise ValueError("金额必须是有限数")
    return int((value * _CENT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_yuan(cents: int) -> Decimal:
    return (Decimal(cents) / _CENT).quantize(Decimal("0.01"))


def format_cents(cents: int, currency: str = "CNY") -> str:
    return f"{cents_to_yuan(cents)} {currency}"


def quantize_quantity(q: Decimal | int | float | str) -> Decimal:
    return Decimal(str(q)).quantize(_Q, rounding=ROUND_HALF_UP)


def split_cents(total_cents: int, weights: list[int]) -> list[int]:
    """按整数权重把 total_cents 拆分，最大余数法，结果之和恒等于 total_cents。

    权重之和必须为正；权重为 0 的份额恒得 0。负数 total（反向分录用）
    先按绝对值拆分再整体取反。
    """
    if not weights or sum(weights) <= 0:
        raise ValueError("权重之和必须为正")
    sign = -1 if total_cents < 0 else 1
    total = abs(total_cents)
    total_weight = sum(weights)
    shares = [total * w // total_weight for w in weights]
    remainder = total - sum(shares)
    # 小数部分最大者先分余；并列时比权重，再比原始位置，保证结果确定
    order = sorted(
        range(len(weights)),
        key=lambda i: ((total * weights[i]) % total_weight, weights[i], -i),
        reverse=True,
    )
    for k in range(remainder):  # remainder < 正权重份额数，零权重不会被选中
        shares[order[k]] += 1
    return [sign * s for s in shares]
