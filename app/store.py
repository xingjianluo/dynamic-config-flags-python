"""配置存储：版本化、原子发布、并发安全、支持回滚。

- 读取只拿不可变快照，热更新期间不会观察到部分生效的中间状态；
- 写入先校验后发布，任何失败都不破坏当前生效配置；
- 写入需携带期望版本号，过期版本被拒绝；
- 回滚把历史版本内容发布为新版本，判定结果与该历史版本一致。
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Any

from app.errors import StaleVersionError, UnknownVersionError
from app.merge import validate_flags, validate_layer

_HISTORY_LIMIT = 50


@dataclass(frozen=True)
class Snapshot:
    """一份不可变的生效配置快照。"""

    version: int
    config: dict[str, Any]
    flags: dict[str, Any]


class ConfigStore:
    """线程安全的版本化配置存储。"""

    def __init__(self, config: dict[str, Any], flags: dict[str, Any]) -> None:
        self._lock = threading.Lock()
        initial = Snapshot(
            version=1,
            config=copy.deepcopy(validate_layer(config)),
            flags=copy.deepcopy(validate_flags(flags)),
        )
        self._current = initial
        self._history: dict[int, Snapshot] = {1: initial}

    def snapshot(self) -> Snapshot:
        """读取当前生效快照（单次原子读取，无中间状态）。"""
        with self._lock:
            snap = self._current
        # 深拷贝返回，调用方无法篡改生效配置
        return Snapshot(snap.version, copy.deepcopy(snap.config), copy.deepcopy(snap.flags))

    def update(
        self,
        config: dict[str, Any] | None = None,
        flags: dict[str, Any] | None = None,
        *,
        expected_version: int,
    ) -> Snapshot:
        """基于期望版本写入（增量合并）。过期版本或非法配置均拒绝。"""
        # 先在校验阶段失败，不触碰当前配置
        new_config_part = validate_layer(config) if config is not None else None
        new_flags_part = validate_flags(flags) if flags is not None else None
        with self._lock:
            if expected_version != self._current.version:
                raise StaleVersionError(expected_version, self._current.version)
            merged_config = dict(self._current.config)
            if new_config_part:
                merged_config.update(new_config_part)
            merged_flags = copy.deepcopy(self._current.flags)
            if new_flags_part:
                merged_flags.update(new_flags_part)
            return self._publish_locked(merged_config, merged_flags)

    def rollback(self, version: int) -> Snapshot:
        """回滚到历史版本，生成新版本号；目标版本不存在则拒绝。"""
        with self._lock:
            target = self._history.get(version)
            if target is None:
                raise UnknownVersionError(version)
            return self._publish_locked(
                copy.deepcopy(target.config), copy.deepcopy(target.flags)
            )

    def history(self) -> list[int]:
        """返回可回滚的历史版本号列表（含当前版本）。"""
        with self._lock:
            return sorted(self._history)

    def _publish_locked(self, config: dict[str, Any], flags: dict[str, Any]) -> Snapshot:
        """在锁内原子发布新版本，并裁剪历史。"""
        snap = Snapshot(self._current.version + 1, config, flags)
        self._current = snap
        self._history[snap.version] = snap
        while len(self._history) > _HISTORY_LIMIT:
            del self._history[min(self._history)]
        return Snapshot(snap.version, copy.deepcopy(config), copy.deepcopy(flags))
