"""健身工作室分润结算领域内核。

模块划分：

- money：金额与课节数量精度
- events：事件类型与稳定标识
- rules：按生效日切换的提成规则版本
- repository：只追加事件库（内存 / JSON 文件）
- engine：事件 -> 分润分录 -> 结算批次的确定性投影
- reports：教练对账与门店对比
- service：对外门面，供 HTTP 层与测试调用
"""

from __future__ import annotations

from .engine import (
    DomainError,
    Entry,
    MaterializedView,
    materialize,
)
from .reports import coach_statement, store_comparison
from .repository import EventStore
from .rules import RuleVersion
from .service import Platform

__all__ = [
    "DomainError",
    "Entry",
    "MaterializedView",
    "Platform",
    "RuleVersion",
    "EventStore",
    "coach_statement",
    "materialize",
    "store_comparison",
]
