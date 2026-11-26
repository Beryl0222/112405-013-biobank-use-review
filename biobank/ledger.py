"""追加式事件账本（JSONL）。

- 每条事件只追加、不修改、不删除；重放事件即可重建全部业务状态。
- 每条决定事件携带 :class:`~biobank.model.Basis` 快照，保留"当时依据什么材料"。
- 链式哈希用于发现被外部改写的账本文件；它不提供防篡改密码学保证，只是完整性检查。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "pack_loaded",
        "historical_allocation_imported",
        "lab_event_recorded",
        "return_recorded",
        "ethics_decision_recorded",
        "allocation_opened",
        "allocation_held",
        "allocation_recalled",
        "allocation_stopped",
        "allocation_reverified",
        "withdrawal_processed",
        "supplemental_consent_registered",
        "sharing_request_decided",
        "note_added",
    }
)


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    event_type: str
    payload: dict[str, Any]
    sources: tuple[str, ...]
    prev_hash: str
    hash: str

    def to_line(self) -> str:
        d = {
            "seq": self.seq,
            "ts": self.ts,
            "event_type": self.event_type,
            "payload": self.payload,
            "sources": list(self.sources),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }
        return json.dumps(d, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_line(cls, line: str) -> "Event":
        d = json.loads(line)
        return cls(
            seq=d["seq"],
            ts=d["ts"],
            event_type=d["event_type"],
            payload=d["payload"],
            sources=tuple(d.get("sources", ())),
            prev_hash=d["prev_hash"],
            hash=d["hash"],
        )


def _hash_of(seq: int, ts: str, event_type: str, payload: dict, sources: list[str], prev_hash: str) -> str:
    body = json.dumps(
        {"seq": seq, "ts": ts, "event_type": event_type, "payload": payload,
         "sources": sources, "prev_hash": prev_hash},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class LedgerIntegrityError(RuntimeError):
    pass


class Ledger:
    """文件型追加日志。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[Event] = []
        if self.path.exists():
            self._load()

    # ------------------------------------------------------------- persistence
    def _load(self) -> None:
        prev = ""
        for lineno, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            event = Event.from_line(line)
            expect = _hash_of(
                event.seq, event.ts, event.event_type, event.payload,
                list(event.sources), event.prev_hash,
            )
            if event.prev_hash != prev:
                raise LedgerIntegrityError(f"第 {lineno} 行断链：prev_hash 不衔接")
            if event.hash != expect:
                raise LedgerIntegrityError(f"第 {lineno} 行哈希不符，账本可能被改写")
            if event.seq != lineno:
                raise LedgerIntegrityError(f"第 {lineno} 行序号不连续")
            prev = event.hash
            self._events.append(event)

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        sources: tuple[str, ...] = (),
        ts: str | None = None,
    ) -> Event:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型 {event_type}")
        seq = len(self._events) + 1
        ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
        prev_hash = self._events[-1].hash if self._events else ""
        digest = _hash_of(seq, ts, event_type, payload, list(sources), prev_hash)
        event = Event(
            seq=seq, ts=ts, event_type=event_type, payload=payload,
            sources=tuple(sources), prev_hash=prev_hash, hash=digest,
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(event.to_line() + "\n")
        self._events.append(event)
        return event

    def replay(self, handler: Callable[[Event], None]) -> None:
        for event in self._events:
            handler(event)

    def verify(self) -> bool:
        prev = ""
        for event in self._events:
            expect = _hash_of(
                event.seq, event.ts, event.event_type, event.payload,
                list(event.sources), event.prev_hash,
            )
            if event.prev_hash != prev or event.hash != expect:
                return False
            prev = event.hash
        return True
