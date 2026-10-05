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
import time
from dataclasses import dataclass
from typing import Any

from app.flags import FlagDecision, FlagRule, evaluate, flags_from_config
from app.layers import Source
from app.schema import ConfigRejected, RejectReason, validate_config

_MISSING = object()  # 差异计算中表示「键不存在」的哨兵


@dataclass(frozen=True)
class DryRunResult:
    """只读预演结果：不改动生效配置、不占版本、不产生审计记录。"""

    accepted: bool
    based_on_version: int                # 预演所基于的生效版本
    reason: str | None = None            # 拒绝时的可区分原因（RejectReason.value）
    detail: str = ""
    changes: dict[str, dict[str, Any]] | None = None            # 会被接受的变更摘要（同审计结构）
    provenance_changes: dict[str, dict[str, Any]] | None = None  # 键 -> {"old": 旧来源, "new": 新来源}


def _diff_provenance(
    old: dict[str, str], new: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """对比两份键级来源，返回发生变化的键及其新旧来源。"""
    changes: dict[str, dict[str, Any]] = {}
    for key in sorted(set(old) | set(new)):
        old_s, new_s = old.get(key), new.get(key)
        if old_s != new_s:
            changes[key] = {"old": old_s, "new": new_s}
    return changes


@dataclass(frozen=True)
class AuditRecord:
    """一次成功运行时变更（含回滚）的审计记录。"""

    version: int            # 本次变更产生的新版本号
    timestamp: float        # 发生时间（epoch 秒）
    kind: str               # "update" | "rollback"
    changes: dict[str, dict[str, Any]]  # 键 -> {"old": 旧值, "new": 新值}，缺失侧为 None 且 missing 为 True
    target_version: int | None = None   # 仅回滚：目标版本号


def _flat_items(config: dict[str, Any]) -> dict[str, Any]:
    """把配置展开成平坦键：普通键原样，flags 展开为 flags.<名称>，便于逐项对比。"""
    flat = {k: v for k, v in config.items() if k != "flags"}
    for name, rule in config.get("flags", {}).items():
        flat[f"flags.{name}"] = rule
    return flat


def _diff_configs(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """计算两份配置的逐项差异；缺失一侧以 {"missing": True, "value": None} 表示。"""
    old_flat, new_flat = _flat_items(old), _flat_items(new)
    changes: dict[str, dict[str, Any]] = {}
    for key in sorted(set(old_flat) | set(new_flat)):
        old_v, new_v = old_flat.get(key, _MISSING), new_flat.get(key, _MISSING)
        if old_v is _MISSING:
            changes[key] = {"old": None, "new": copy.deepcopy(new_v), "old_missing": True, "new_missing": False}
        elif new_v is _MISSING:
            changes[key] = {"old": copy.deepcopy(old_v), "new": None, "old_missing": False, "new_missing": True}
        elif old_v != new_v:
            changes[key] = {"old": copy.deepcopy(old_v), "new": copy.deepcopy(new_v), "old_missing": False, "new_missing": False}
    return changes


@dataclass(frozen=True)
class Snapshot:
    """某一版本的不可变配置快照。"""

    version: int
    config: dict[str, Any]
    rules: dict[str, FlagRule]
    provenance: dict[str, str]  # 键级来源（含 flags.<名称> 逐项），与 config 同生同灭


def _expand_provenance(base: dict[str, str], config: dict[str, Any]) -> dict[str, str]:
    """把键级来源展开到开关逐项：flags 整表同来源，故 flags.<名称> 与 flags 一致。"""
    provenance = dict(base)
    flag_source = base.get("flags")
    if flag_source is not None:
        for flag_name in config.get("flags", {}):
            provenance[f"flags.{flag_name}"] = flag_source
    return provenance


class ConfigStore:
    """线程安全的版本化配置存储。"""

    def __init__(
        self,
        initial: dict[str, Any] | None = None,
        provenance: dict[str, str] | None = None,
        audit_capacity: int = 100,
    ) -> None:
        if audit_capacity < 1:
            raise ValueError("audit_capacity 必须 >= 1")
        self._audit_capacity = audit_capacity
        first = validate_config(initial or {})
        # 构造时未指明来源的键按 DEFAULT 对待（等价于内置默认值层）
        base_prov = {key: (provenance or {}).get(key, Source.DEFAULT.name) for key in first}
        self._lock = threading.RLock()
        self._snapshot = Snapshot(
            0, copy.deepcopy(first), flags_from_config(first), _expand_provenance(base_prov, first)
        )
        # 历史版本（含当前版本），version -> (配置内容, 键级来源)；内容一经写入不再修改。
        # 容量与审计上限联动：最多保留 audit_capacity + 1 个版本（当前版本 + 每次成功变更一条），
        # 超出后按版本从旧到新淘汰，被淘汰版本不可再作为回滚目标。
        self._history: dict[int, tuple[dict[str, Any], dict[str, str]]] = {
            0: (copy.deepcopy(first), dict(self._snapshot.provenance))
        }
        # 审计日志：仅记录成功的运行时变更（含回滚），按时间先后排列，超容量淘汰最旧
        self._audit: list[AuditRecord] = []

    def _record_audit(self, record: AuditRecord) -> None:
        """追加审计记录并按容量淘汰最旧；同步淘汰最旧历史版本（调用方须持锁）。"""
        self._audit.append(record)
        if len(self._audit) > self._audit_capacity:
            del self._audit[0]
        while len(self._history) > self._audit_capacity + 1:
            oldest = min(self._history)
            if oldest == self._snapshot.version:
                break  # 当前版本永不被淘汰
            del self._history[oldest]

    def get_audit_log(self) -> list[AuditRecord]:
        """返回审计记录的深拷贝（按时间从旧到新），调用方改动不影响内部状态。"""
        with self._lock:
            return copy.deepcopy(self._audit)

    def dry_run(self, raw: dict[str, Any], expected_version: int) -> DryRunResult:
        """只读预演：校验候选变更并汇总影响，不改生效配置、不占版本、不写审计。

        全程基于同一份不可变快照：并发真实更新不会让预演看到半新半旧的中间态，
        预演本身也不会干扰生效配置；返回数据均为新建副本，调用方改动不影响存储。
        """
        snapshot = self._snapshot  # 引用读取，天然对应某一完整版本
        try:
            validated = validate_config(raw)  # 纯函数校验，不触碰任何状态
        except ConfigRejected as exc:
            return DryRunResult(
                accepted=False,
                based_on_version=snapshot.version,
                reason=exc.reason.value,
                detail=exc.detail,
            )
        if snapshot.version != expected_version:
            return DryRunResult(
                accepted=False,
                based_on_version=snapshot.version,
                reason=RejectReason.STALE_VERSION.value,
                detail=f"期望基于版本 {expected_version} 写入，当前版本为 {snapshot.version}",
            )
        frozen = copy.deepcopy(validated)
        new_prov = _expand_provenance({key: Source.RUNTIME.name for key in frozen}, frozen)
        return DryRunResult(
            accepted=True,
            based_on_version=snapshot.version,
            detail="候选变更可被接受",
            changes=_diff_configs(snapshot.config, frozen),
            provenance_changes=_diff_provenance(snapshot.provenance, new_prov),
        )

    @property
    def version(self) -> int:
        return self._snapshot.version

    @property
    def audit_capacity(self) -> int:
        return self._audit_capacity

    def get_config(self) -> dict[str, Any]:
        """返回当前生效配置的深拷贝，调用方无法通过返回值改动内部状态。"""
        return copy.deepcopy(self._snapshot.config)

    def get_provenance(self) -> dict[str, str]:
        """当前生效配置的键级来源（含 flags.<名称>），与生效值同步更新。"""
        return dict(self._snapshot.provenance)

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
            # 整份运行时写入：生效键全部来自 RUNTIME 层，来源与值同步切换
            prov = _expand_provenance(
                {key: Source.RUNTIME.name for key in frozen}, frozen
            )
            self._history[new_version] = (frozen, prov)
            self._snapshot = Snapshot(new_version, frozen, flags_from_config(frozen), prov)
            self._record_audit(AuditRecord(
                version=new_version,
                timestamp=time.time(),
                kind="update",
                changes=_diff_configs(current.config, frozen),
            ))
            return new_version

    def rollback(self, target_version: int) -> int:
        """回滚到历史版本：用其配置内容与来源生成一个新版本并原子生效。

        目标版本从未存在（未来版本/负数）-> VERSION_NOT_FOUND；
        目标版本曾存在但已超容量被淘汰 -> VERSION_EVICTED，两者可区分。
        """
        with self._lock:
            current = self._snapshot
            entry = self._history.get(target_version)
            if entry is None:
                if 0 <= target_version <= current.version:
                    raise ConfigRejected(
                        RejectReason.VERSION_EVICTED,
                        f"历史版本 {target_version} 已超审计容量被淘汰，无法回滚；"
                        f"当前可回滚范围 {self.history_versions()}",
                    )
                raise ConfigRejected(
                    RejectReason.VERSION_NOT_FOUND,
                    f"历史版本 {target_version} 不存在，当前最新版本 {current.version}",
                )
            old_config, old_prov = entry
            new_version = current.version + 1
            restored = copy.deepcopy(old_config)
            prov = dict(old_prov)
            self._history[new_version] = (restored, prov)
            self._snapshot = Snapshot(new_version, restored, flags_from_config(restored), prov)
            self._record_audit(AuditRecord(
                version=new_version,
                timestamp=time.time(),
                kind="rollback",
                changes=_diff_configs(current.config, restored),
                target_version=target_version,
            ))
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
