"""版本化配置存储：乐观并发、原子热更新、回滚、来源追溯、变更审计与只读预演。

一致性保证：
- 生效状态是不可变快照 (Snapshot)，热更新只在锁内完成「构建新快照 -> 整体替换引用」，
  因此并发读取永远看到某一完整版本，不会观察到部分生效的中间状态。
- 每个快照同时携带 config 与 provenance，二者在锁内一起替换，
  故来源说明永远和当前生效值对得上，热更新/回滚后不会停留在旧来源。
- 写入先在锁外完成校验（不触碰任何状态），只有校验全部通过后才进入临界区，
  故任何非法配置都不会破坏当前生效配置，失败时继续沿用上一份可用配置；
  失败的写入不占版本、不产生审计记录。
- 每次写入携带 expected_version（乐观锁）；与当前版本不一致即拒绝为 STALE_VERSION。
- 回滚用历史版本内容生成新的版本号，旧版本保持不可变，判定结果与目标版本一致；
  历史版本受 history_capacity 约束淘汰，目标被淘汰时拒绝为 VERSION_EVICTED，
  与「从未存在过」的 VERSION_NOT_FOUND 明确区分。
- preview 只读取某个完整快照做校验与差异计算，不修改任何状态，
  不占版本、不留审计；并发真实更新不会让预演看到半新半旧的中间状态。
"""
from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.audit import AuditLog, AuditRecord
from app.diffing import diff_configs
from app.flags import FlagDecision, FlagRule, evaluate, flags_from_config
from app.layers import Source
from app.schema import ConfigRejected, RejectReason, validate_config


@dataclass(frozen=True)
class EffectiveView:
    """一次查询看到的完整版本视图（值均为独立深拷贝）。"""

    version: int
    config: dict[str, Any]
    provenance: dict[str, str]


def _normalize_provenance(config: dict[str, Any], provenance: dict[str, str]) -> dict[str, str]:
    """补全来源说明：每个生效键都要有来源；flags 细化到每个开关。

    - 未显式记录来源的键按 DEFAULT 处理（如直接构造 ConfigStore 的单测场景）；
    - flags 整表来自某一层时，flags.<名称> 继承同一来源；
    - 仅保留当前配置中实际存在的键，保证来源与生效值一一对应。
    """
    result: dict[str, str] = {}
    for key in config:
        if key == "flags":
            continue
        result[key] = provenance.get(key, Source.DEFAULT.name)
    flags_source = provenance.get("flags", Source.DEFAULT.name)
    if "flags" in config:
        result["flags"] = flags_source
        for name in config["flags"]:
            result[f"flags.{name}"] = provenance.get(f"flags.{name}", flags_source)
    return result


def _runtime_provenance(config: dict[str, Any]) -> dict[str, str]:
    """运行时整份写入：所有生效键（含每个开关）来源均为 RUNTIME。"""
    provenance = {key: Source.RUNTIME.name for key in config if key != "flags"}
    if "flags" in config:
        provenance["flags"] = Source.RUNTIME.name
        provenance.update({f"flags.{name}": Source.RUNTIME.name for name in config["flags"]})
    return provenance


@dataclass(frozen=True)
class Snapshot:
    """某一版本的不可变配置快照。"""

    version: int
    config: dict[str, Any]
    rules: dict[str, FlagRule]
    provenance: dict[str, str]


class ConfigStore:
    """线程安全的版本化配置存储。"""

    def __init__(
        self,
        initial: dict[str, Any] | None = None,
        initial_provenance: dict[str, str] | None = None,
        history_capacity: int = 64,
        audit_capacity: int = 100,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if history_capacity < 1:
            raise ValueError("history_capacity 必须 >= 1")
        first = validate_config(initial or {})
        self._lock = threading.RLock()
        provenance = _normalize_provenance(first, dict(initial_provenance or {}))
        self._snapshot = Snapshot(
            0, copy.deepcopy(first), flags_from_config(first), dict(provenance)
        )
        # 历史版本（含当前版本）：version -> (配置内容, 来源说明)；内容一经写入不再修改
        self._history: dict[int, tuple[dict[str, Any], dict[str, str]]] = {
            0: (copy.deepcopy(first), dict(provenance))
        }
        self._history_capacity = history_capacity
        self._audit = AuditLog(capacity=audit_capacity, clock=clock)

    @property
    def version(self) -> int:
        return self._snapshot.version

    def get_config(self) -> dict[str, Any]:
        """返回当前生效配置的深拷贝，调用方无法通过返回值改动内部状态。"""
        return copy.deepcopy(self._snapshot.config)

    def get_provenance(self) -> dict[str, str]:
        """返回当前生效配置每个键的来源说明（独立拷贝，与当前生效值严格对应）。"""
        return dict(self._snapshot.provenance)

    def get_effective(self) -> EffectiveView:
        """一次性返回 (版本, 生效配置, 来源)，三者来自同一不可变快照且均为独立拷贝。

        分开调用 get_config/get_provenance 理论上可能跨过一次热更新，
        需要值与来源严格配套时使用本方法。
        """
        snapshot = self._snapshot
        return EffectiveView(
            version=snapshot.version,
            config=copy.deepcopy(snapshot.config),
            provenance=dict(snapshot.provenance),
        )

    def audit_records(self) -> list[AuditRecord]:
        """返回审计记录（按时间正序，独立拷贝）；只含成功的运行时变更。"""
        with self._lock:
            return self._audit.records()

    def preview(self, raw: dict[str, Any], expected_version: int | None = None) -> dict[str, Any]:
        """只读预演一次整份写入，不改变生效配置、不占版本、不产生审计。

        - 配置非法：accepted=False，reason 为 unknown_key/type_mismatch/out_of_range；
        - 指定 expected_version 且与当前不一致：accepted=False，reason=stale_version；
        - 可接受：accepted=True，给出 would_be_version 与结构化变化摘要
          （哪些键/开关会变、变更后来源会怎样）。
        所有计算基于同一完整快照的深拷贝，并发更新不会让预演看到中间状态。
        """
        # 锁外先做纯函数校验，不触碰任何共享状态
        try:
            validated = validate_config(raw)
        except ConfigRejected as exc:
            return {
                "accepted": False,
                "reason": exc.reason.value,
                "detail": exc.detail,
                "base_version": self._snapshot.version,
                "would_be_version": None,
                "changes": None,
                "provenance_after": None,
            }
        with self._lock:
            current = self._snapshot
            if expected_version is not None and current.version != expected_version:
                return {
                    "accepted": False,
                    "reason": RejectReason.STALE_VERSION.value,
                    "detail": (
                        f"期望基于版本 {expected_version} 预演，当前版本为 {current.version}"
                    ),
                    "base_version": current.version,
                    "would_be_version": None,
                    "changes": None,
                    "provenance_after": None,
                }
            candidate = copy.deepcopy(validated)
            changes = diff_configs(current.config, candidate)
            provenance_after = _runtime_provenance(candidate)
            return {
                "accepted": True,
                "reason": None,
                "detail": None,
                "base_version": current.version,
                # 仅预演，不占用版本号；这里仅展示「若提交成功会成为哪个版本」
                "would_be_version": current.version + 1,
                "changes": copy.deepcopy(changes),
                "provenance_after": dict(provenance_after),
            }

    def update(self, raw: dict[str, Any], expected_version: int) -> int:
        """基于 expected_version 提交整份配置。

        - 校验失败：抛出 ConfigRejected（UNKNOWN_KEY / TYPE_MISMATCH / OUT_OF_RANGE），
          当前版本保持不变。
        - 版本过期：抛出 ConfigRejected(STALE_VERSION)，当前版本保持不变。
        失败不产生审计记录、不占用版本号；成功返回新版本号并登记审计。
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
            provenance = _runtime_provenance(frozen)
            changes = diff_configs(current.config, frozen)
            self._commit(
                new_version, frozen, provenance,
                kind="update", from_version=current.version, changes=changes,
            )
            return new_version

    def rollback(self, target_version: int) -> int:
        """回滚到历史版本：用其配置内容与来源生成新版本并原子生效。

        - 目标版本仍在保留窗口内：成功，返回新版本号并登记审计；
        - 目标版本号从未存在：VERSION_NOT_FOUND；
        - 目标版本曾存在但已被容量上限淘汰：VERSION_EVICTED（可区分，绝不静默回滚到别处）。
        """
        with self._lock:
            current = self._snapshot
            old_entry = self._history.get(target_version)
            if old_entry is None:
                latest = max(self._history)
                if 0 <= target_version < latest:
                    raise ConfigRejected(
                        RejectReason.VERSION_EVICTED,
                        f"历史版本 {target_version} 已被淘汰（保留最近 {self._history_capacity} 个版本）",
                    )
                raise ConfigRejected(
                    RejectReason.VERSION_NOT_FOUND,
                    f"历史版本 {target_version} 不存在，当前最新版本 {current.version}",
                )
            new_version = current.version + 1
            restored = copy.deepcopy(old_entry[0])
            provenance = dict(old_entry[1])
            changes = diff_configs(current.config, restored)
            self._commit(
                new_version, restored, provenance,
                kind="rollback", from_version=current.version, changes=changes,
                target_version=target_version,
            )
            return new_version

    def _commit(
        self,
        new_version: int,
        config: dict[str, Any],
        provenance: dict[str, str],
        *,
        kind: str,
        from_version: int,
        changes: dict[str, Any],
        target_version: int | None = None,
    ) -> None:
        """锁内：存历史 -> 登记审计 -> 原子替换快照；随后按容量淘汰最旧历史版本。"""
        self._history[new_version] = (config, dict(provenance))
        self._audit.append(
            kind, from_version, new_version, changes, target_version=target_version
        )
        self._snapshot = Snapshot(
            new_version, config, flags_from_config(config), dict(provenance)
        )
        while len(self._history) > self._history_capacity:
            oldest = min(self._history)
            del self._history[oldest]

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
