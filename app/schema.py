"""配置与功能开关的模式定义：已知键、类型与取值范围。

任何不在本模式中的键都会被拒绝（unknown_key），
类型或取值不满足规则的键也会被拒绝（type_mismatch / out_of_range）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


@dataclass(frozen=True)
class FieldSpec:
    """单个配置键的校验规则。"""

    type_: type
    min_value: float | None = None
    max_value: float | None = None
    choices: frozenset[Any] | None = None


# 已知配置键及其校验规则
CONFIG_SCHEMA: dict[str, FieldSpec] = {
    "service.name": FieldSpec(str),
    "service.port": FieldSpec(int, min_value=1, max_value=65535),
    "service.log_level": FieldSpec(str, choices=LOG_LEVELS),
    "limits.max_requests": FieldSpec(int, min_value=1, max_value=1_000_000),
    "limits.timeout_ms": FieldSpec(int, min_value=1, max_value=60_000),
    "features.enable_metrics": FieldSpec(bool),
}


@dataclass(frozen=True)
class FlagRule:
    """单个功能开关的规则：总开关、灰度比例、名单。"""

    enabled: bool
    rollout: int  # 0..100，百分比
    allowlist: tuple[str, ...] = ()
    denylist: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rollout": self.rollout,
            "allowlist": list(self.allowlist),
            "denylist": list(self.denylist),
        }


# 已知功能开关及其默认规则
FLAG_SPECS: dict[str, FlagRule] = {
    "new_checkout": FlagRule(enabled=True, rollout=0),
    "dark_mode": FlagRule(enabled=False, rollout=0),
    "beta_api": FlagRule(enabled=False, rollout=0, allowlist=()),
}
