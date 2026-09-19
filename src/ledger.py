"""事件台账：所有业务事实以不可变事件的形式仅追加（JSONL）。

状态一律通过回放台账重建，不做原地更新。退款 / 坏账 / 更正都以新事件
（反向分录）存在，原事件永不删除。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

LEDGER_FILE = "events.jsonl"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Event:
    """台账事件。seq 为追加时分配的台账内自增序号。"""

    __slots__ = ("seq", "id", "type", "payload", "recorded_at")

    def __init__(
        self, seq: int, event_id: str, event_type: str, payload: dict[str, Any], recorded_at: str
    ) -> None:
        self.seq = seq
        self.id = event_id
        self.type = event_type
        self.payload = payload
        self.recorded_at = recorded_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "id": self.id,
            "type": self.type,
            "payload": self.payload,
            "recorded_at": self.recorded_at,
        }


class Ledger:
    """线程安全的仅追加 JSONL 台账。"""

    def __init__(self, directory: str | os.PathLike[str] = ".runtime") -> None:
        self.path = Path(directory) / LEDGER_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._reload()

    def _reload(self) -> None:
        self._events.clear()
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                self._events.append(
                    Event(raw["seq"], raw["id"], raw["type"], raw["payload"], raw["recorded_at"])
                )

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def head_seq(self) -> int:
        with self._lock:
            return self._events[-1].seq if self._events else 0

    def append(self, event_type: str, payload: dict[str, Any], event_id: str | None = None) -> Event:
        """追加一条事件。event_id 由调用方给定（保证重放/重试幂等）或自动生成。"""
        with self._lock:
            event_id = event_id or new_id("evt")
            seq = self.head_seq() + 1
            recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            event = Event(seq, event_id, event_type, payload, recorded_at)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
            self._events.append(event)
            return event

    def has_id(self, event_id: str) -> bool:
        with self._lock:
            return any(e.id == event_id for e in self._events)

    def iter_type(self, event_type: str) -> Iterable[Event]:
        return (e for e in self.events if e.type == event_type)
