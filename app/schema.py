"""配置模式定义：字段类型、取值范围与可区分的拒绝原因。"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class RejectReason(enum.Enum):
    """配置被拒绝的可区分原因。"""

    UNKNOWN_KEY = "unknown_key"          # 未知键
    TYPE_MISMATCH = "type_mismatch"      # 类型不匹配
    OUT_OF_RANGE = "out_of_range"        # 取值越界
    STALE_VERSION = "stale_version"      # 基于过期版本的写入
    VERSION_NOT_FOUND = "version_not_found"  # 回滚目标版本不存在（从未有过）
    VERSION_EVICTED = "version_evicted"  # 回滚目标版本曾存在但已被容量淘汰


class ConfigRejected(Exception):
    """配置校验失败异常，携带可区分原因与明细。"""

    def __init__(self, reason: RejectReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


@dataclass(frozen=True)
class FieldSpec:
    """单个配置字段的约束定义。"""

    name: str
    type_: type
    min_value: float | None = None
    max_value: float | None = None
    default: Any = None

    def validate(self, value: Any) -> Any:
        """校验单个值，失败抛出 ConfigRejected，成功返回原值。"""
        # bool 是 int 的子类，需排除，避免 true 被当作数值字段接受
        if isinstance(value, bool) and self.type_ is not bool:
            raise ConfigRejected(
                RejectReason.TYPE_MISMATCH,
                f"字段 {self.name} 期望 {self.type_.__name__}，得到 bool",
            )
        if not isinstance(value, self.type_):
            raise ConfigRejected(
                RejectReason.TYPE_MISMATCH,
                f"字段 {self.name} 期望 {self.type_.__name__}，得到 {type(value).__name__}",
            )
        if isinstance(value, (int, float)):
            if self.min_value is not None and value < self.min_value:
                raise ConfigRejected(
                    RejectReason.OUT_OF_RANGE,
                    f"字段 {self.name} 取值 {value} 小于下限 {self.min_value}",
                )
            if self.max_value is not None and value > self.max_value:
                raise ConfigRejected(
                    RejectReason.OUT_OF_RANGE,
                    f"字段 {self.name} 取值 {value} 大于上限 {self.max_value}",
                )
        return value


# 开关规则字段的类型约束（flags.<名称>.<字段>）
FLAG_FIELD_TYPES: dict[str, type] = {
    "enabled": bool,
    "percentage": (int, float),  # type: ignore[assignment]
    "allowlist": list,
}


def _validate_flag(name: str, rule: Any) -> None:
    """校验单个开关规则结构。"""
    if not isinstance(rule, dict):
        raise ConfigRejected(
            RejectReason.TYPE_MISMATCH,
            f"开关 {name} 期望对象，得到 {type(rule).__name__}",
        )
    for key, value in rule.items():
        if key not in FLAG_FIELD_TYPES:
            raise ConfigRejected(
                RejectReason.UNKNOWN_KEY, f"开关 {name} 含未知字段 {key}"
            )
        expected = FLAG_FIELD_TYPES[key]
        if isinstance(value, bool) and expected is not bool:
            raise ConfigRejected(
                RejectReason.TYPE_MISMATCH,
                f"开关 {name}.{key} 期望数值/列表，得到 bool",
            )
        if not isinstance(value, expected):
            raise ConfigRejected(
                RejectReason.TYPE_MISMATCH,
                f"开关 {name}.{key} 类型不匹配，得到 {type(value).__name__}",
            )
        if key == "percentage" and not (0 <= value <= 100):
            raise ConfigRejected(
                RejectReason.OUT_OF_RANGE,
                f"开关 {name}.percentage 取值 {value} 越界 [0, 100]",
            )
        if key == "allowlist" and not all(isinstance(s, str) for s in value):
            raise ConfigRejected(
                RejectReason.TYPE_MISMATCH,
                f"开关 {name}.allowlist 元素必须均为字符串",
            )


# 服务内置配置模式
CONFIG_SCHEMA: dict[str, FieldSpec] = {
    "service_name": FieldSpec("service_name", str, default="dynamic-config-service"),
    "max_connections": FieldSpec("max_connections", int, min_value=1, max_value=65535, default=100),
    "request_timeout_ms": FieldSpec("request_timeout_ms", int, min_value=1, max_value=600000, default=3000),
    "debug": FieldSpec("debug", bool, default=False),
}


def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """按模式校验整份配置。

    任一字段失败即整体拒绝（抛出 ConfigRejected），调用方保证沿用上一份可用配置。
    返回仅包含已校验键的规范化副本。
    """
    if not isinstance(raw, dict):
        raise ConfigRejected(RejectReason.TYPE_MISMATCH, "配置整体必须是对象")
    validated: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "flags":
            if not isinstance(value, dict):
                raise ConfigRejected(RejectReason.TYPE_MISMATCH, "flags 必须是对象")
            for flag_name, rule in value.items():
                _validate_flag(flag_name, rule)
            validated[key] = value
            continue
        spec = CONFIG_SCHEMA.get(key)
        if spec is None:
            raise ConfigRejected(RejectReason.UNKNOWN_KEY, f"未知配置键 {key}")
        validated[key] = spec.validate(value)
    return validated
