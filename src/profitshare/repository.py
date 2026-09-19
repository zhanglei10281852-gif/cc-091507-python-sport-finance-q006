"""只追加事件库。

- append 对 event_id 幂等：同一 ID 同一内容重复写入返回 False，绝不产生第二条；
  同 ID 不同内容直接报错，防止撞号污染。
- 持久化为单个 JSON 文件，先写临时文件再原子替换。
- 读取顺序即追加顺序；投影严格按此顺序重放。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .events import Event


class EventConflictError(Exception):
    pass


class EventStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._events: list[Event] = []
        self._index: dict[str, Event] = {}
        self._lock = threading.RLock()
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._load()

    # ----- 基本操作 -----

    def append(self, event: Event) -> bool:
        """追加事件。返回 True 表示新写入，False 表示幂等命中。"""
        with self._lock:
            existing = self._index.get(event.event_id)
            if existing is not None:
                if existing.to_dict() != event.to_dict():
                    raise EventConflictError(
                        f"事件标识冲突且内容不一致: {event.event_id}"
                    )
                return False
            self._events.append(event)
            self._index[event.event_id] = event
            self._persist()
            return True

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            return self._index.get(event_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    # ----- 持久化 -----

    def _load(self) -> None:
        assert self._path is not None
        with self._path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for raw in data.get("events", []):
            event = Event.from_dict(raw)
            if event.event_id in self._index:
                raise EventConflictError(f"持久化数据中事件标识重复: {event.event_id}")
            self._events.append(event)
            self._index[event.event_id] = event

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "format": "profitshare-event-log/1",
            "events": [e.to_dict() for e in self._events],
        }
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self._path)
