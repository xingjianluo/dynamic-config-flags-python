"""版本化配置存储：乐观并发、原子热更新、回滚。

一致性保证：
- 生效状态是不可变快照 (Snapshot)，热更新只在锁内完成「构建新快照 -> 整体替换引用」，
  因此并发读取永远看到某一完整版本，不会观察到部分生效的中间状态。
- 写入先在锁外完成校验（不触碰任何状态），只有校验全部通过后才进入临界区，
  故任何非法配置都不会破坏当前生效配置，失败时继续沿用上一份可用配置。
- 每次写入携带 expected_version（乐观锁）；与当前版本不一致即拒绝为 STALE_VERSION。
- 回滚用历史版本内容生成新的版本号，旧版本保持不可变，判定结果与目标版本一致。
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Any

from app.flags import FlagDecision, FlagRule, evaluate, flags_from_config
from app.schema import ConfigRejected, RejectReason, validate_config


@dataclass(frozen=True)
class Snapshot:
    """某一版本的不可变配置快照。"""

    version: int
    config: dict[str, Any]
    rules: dict[str, FlagRule]


class ConfigStore:
    """线程安全的版本化配置存储。"""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        first = validate_config(initial or {})
        self._lock = threading.RLock()
        self._snapshot = Snapshot(0, copy.deepcopy(first), flags_from_config(first))
        # 历史版本（含当前版本），version -> 配置内容；内容一经写入不再修改
        self._history: dict[int, dict[str, Any]] = {0: copy.deepcopy(first)}

    @property
    def version(self) -> int:
        return self._snapshot.version

    def get_config(self) -> dict[str, Any]:
        """返回当前生效配置的深拷贝，调用方无法通过返回值改动内部状态。"""
        return copy.deepcopy(self._snapshot.config)

    def get_provenance(self) -> dict[str, str]:
        """键级来源说明（初始分层加载时记录），可解释优先级。"""
        return dict(self._snapshot.config.get("__provenance__", {}))  # 占位，实际见 loader

    def update(self, raw: dict[str, Any], expected_version: int) -> int:
        """基于 expected_version 提交整份配置。

        - 校验失败：抛出 ConfigRejected（UNKNOWN_KEY / TYPE_MISMATCH / OUT_OF_RANGE），
          当前版本保持不变。
        - 版本过期：抛出 ConfigRejected(STALE_VERSION)，当前版本保持不变。
        成功返回新版本号。
        """
        validated = validate_config(raw)  # 锁外校验，不触碰状态
        with self._lock:
            current = self._snapshot
            if current.version != expected_version:
                raise ConfigRejected(
                    RejectReason.STALE_VERSION,
                    f"期望基于版本 {expected_version} 写入，当前版本为 {current.version}",
                )
            new_version = current.version + 1
            frozen = copy.deepcopy(validated)
            self._history[new_version] = frozen
            self._snapshot = Snapshot(new_version, frozen, flags_from_config(frozen))
            return new_version

    def rollback(self, target_version: int) -> int:
        """回滚到历史版本：用其配置内容生成一个新版本并原子生效。"""
        with self._lock:
            current = self._snapshot
            old_config = self._history.get(target_version)
            if old_config is None:
                raise ConfigRejected(
                    RejectReason.VERSION_NOT_FOUND,
                    f"历史版本 {target_version} 不存在，当前最新版本 {current.version}",
                )
            new_version = current.version + 1
            restored = copy.deepcopy(old_config)
            self._history[new_version] = restored
            self._snapshot = Snapshot(new_version, restored, flags_from_config(restored))
            return new_version

    def history_versions(self) -> list[int]:
        with self._lock:
            return sorted(self._history)

    def snapshot(self) -> Snapshot:
        """获取当前不可变快照引用；读取期间内容永不会被热更新修改。"""
        return self._snapshot

    def check_flag(self, flag: str, subject: str) -> FlagDecision:
        """读取当前不可变快照做判定；未知开关按 fail-closed 拒绝。"""
        snapshot = self._snapshot  # 引用读取，天然对应某一完整版本
        rule = snapshot.rules.get(flag)
        if rule is None:
            return FlagDecision(
                flag, subject, False, f"未知开关 {flag}（版本 {snapshot.version}，fail-closed）"
            )
        return evaluate(rule, subject)
