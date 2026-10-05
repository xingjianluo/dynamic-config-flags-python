"""变更审计：仅记录成功的运行时变更（含回滚），容量超限按时间淘汰最旧记录。

- 只有成功的 update / rollback 才会产生审计记录；被拒绝或失败的写入不会留下任何痕迹。
- 记录按写入顺序编号（sequence 单调递增），容量超限时淘汰最旧的一条。
- append 入参与 records 返回值都做深拷贝，调用方改动返回值无法影响日志内部状态。
"""
from __future__ import annotations

import copy
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def _iso_now() -> str:
    """UTC ISO-8601 时间，便于按时间排序与事后排查。"""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AuditRecord:
    """一条成功运行时变更的审计记录。"""

    sequence: int                 # 单调递增序号，也是记录的先后次序
    timestamp: str                # ISO-8601 发生时间
    kind: str                     # update | rollback
    from_version: int             # 变更前生效版本
    version: int                  # 本次变更产生的新版本
    changes: dict[str, Any]       # 本次到底改了什么（结构化差异）
    target_version: int | None = None  # kind=rollback 时的回滚目标版本


class AuditLog:
    """有容量上限的审计日志，超限淘汰最旧记录；所有返回值均为独立拷贝。"""

    def __init__(self, capacity: int = 100, clock: Callable[[], str] | None = None) -> None:
        if capacity < 1:
            raise ValueError("audit capacity 必须 >= 1")
        self._capacity = capacity
        self._clock = clock or _iso_now
        self._records: deque[AuditRecord] = deque(maxlen=capacity)
        self._sequence = 0

    def append(
        self,
        kind: str,
        from_version: int,
        version: int,
        changes: dict[str, Any],
        target_version: int | None = None,
    ) -> AuditRecord:
        """登记一条成功变更；容量超限时自动淘汰最旧记录。返回记录的独立拷贝。"""
        self._sequence += 1
        record = AuditRecord(
            sequence=self._sequence,
            timestamp=self._clock(),
            kind=kind,
            from_version=from_version,
            version=version,
            changes=copy.deepcopy(changes),
            target_version=target_version,
        )
        self._records.append(record)
        return copy.deepcopy(record)

    def records(self) -> list[AuditRecord]:
        """按时间正序（旧 -> 新）返回全部记录的独立拷贝。"""
        return copy.deepcopy(list(self._records))

    @property
    def capacity(self) -> int:
        return self._capacity
