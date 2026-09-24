"""配置校验与分层合并。

校验失败按原因区分：unknown_key / type_mismatch / out_of_range，
任何一层校验失败则整体拒绝，调用方保证沿用上一份可用配置。
"""

from __future__ import annotations

from typing import Any

from app.errors import ConfigRejectedError, RejectionReason
from app.schema import CONFIG_SCHEMA, FLAG_SPECS, FieldSpec
from app.sources import SourceLayer


def _check_value(key: str, value: Any, spec: FieldSpec) -> None:
    """校验单个值，失败抛出带区分原因的 ConfigRejectedError。"""
    # bool 是 int 的子类，需精确区分
    if spec.type_ is bool:
        ok = isinstance(value, bool)
    elif spec.type_ is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, spec.type_)
    if not ok:
        raise ConfigRejectedError(
            RejectionReason.TYPE_MISMATCH,
            [key],
            f"expect {spec.type_.__name__}, got {type(value).__name__}: {value!r}",
        )
    if spec.choices is not None and value not in spec.choices:
        raise ConfigRejectedError(
            RejectionReason.OUT_OF_RANGE,
            [key],
            f"{value!r} not in {sorted(spec.choices)}",
        )
    if spec.min_value is not None and value < spec.min_value:
        raise ConfigRejectedError(
            RejectionReason.OUT_OF_RANGE, [key], f"{value!r} < {spec.min_value}"
        )
    if spec.max_value is not None and value > spec.max_value:
        raise ConfigRejectedError(
            RejectionReason.OUT_OF_RANGE, [key], f"{value!r} > {spec.max_value}"
        )


def validate_layer(config: dict[str, Any]) -> dict[str, Any]:
    """校验单层配置，返回规范化副本。未知键/类型不匹配/越界均拒绝。"""
    unknown = [k for k in config if k not in CONFIG_SCHEMA]
    if unknown:
        raise ConfigRejectedError(
            RejectionReason.UNKNOWN_KEY, sorted(unknown), "keys not in schema"
        )
    for key, value in config.items():
        _check_value(key, value, CONFIG_SCHEMA[key])
    return dict(config)


def merge_layers(layers: list[tuple[SourceLayer, dict[str, Any]]]) -> dict[str, Any]:
    """按优先级合并各层：层级数值大者覆盖小者。每层先校验再合并。"""
    merged: dict[str, Any] = {}
    for layer, config in sorted(layers, key=lambda item: item[0]):
        merged.update(validate_layer(config))
    return merged


def validate_flags(flags: dict[str, Any]) -> dict[str, Any]:
    """校验开关配置，返回规范化副本（名单去重排序，保证可复现）。"""
    unknown = [k for k in flags if k not in FLAG_SPECS]
    if unknown:
        raise ConfigRejectedError(
            RejectionReason.UNKNOWN_KEY, sorted(unknown), "unknown flags"
        )
    normalized: dict[str, Any] = {}
    for name, rule in flags.items():
        if not isinstance(rule, dict):
            raise ConfigRejectedError(
                RejectionReason.TYPE_MISMATCH, [name], "flag rule must be an object"
            )
        unknown_fields = [k for k in rule if k not in ("enabled", "rollout", "allowlist", "denylist")]
        if unknown_fields:
            raise ConfigRejectedError(
                RejectionReason.UNKNOWN_KEY,
                [f"{name}.{k}" for k in sorted(unknown_fields)],
                "unknown flag fields",
            )
        enabled = rule.get("enabled", FLAG_SPECS[name].enabled)
        rollout = rule.get("rollout", FLAG_SPECS[name].rollout)
        allowlist = rule.get("allowlist", list(FLAG_SPECS[name].allowlist))
        denylist = rule.get("denylist", list(FLAG_SPECS[name].denylist))
        if not isinstance(enabled, bool):
            raise ConfigRejectedError(
                RejectionReason.TYPE_MISMATCH, [f"{name}.enabled"], "expect bool"
            )
        if not isinstance(rollout, int) or isinstance(rollout, bool):
            raise ConfigRejectedError(
                RejectionReason.TYPE_MISMATCH, [f"{name}.rollout"], "expect int"
            )
        if not 0 <= rollout <= 100:
            raise ConfigRejectedError(
                RejectionReason.OUT_OF_RANGE, [f"{name}.rollout"], "expect 0..100"
            )
        for field_name, value in (("allowlist", allowlist), ("denylist", denylist)):
            if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
                raise ConfigRejectedError(
                    RejectionReason.TYPE_MISMATCH,
                    [f"{name}.{field_name}"],
                    "expect list[str]",
                )
        normalized[name] = {
            "enabled": enabled,
            "rollout": rollout,
            "allowlist": sorted(set(allowlist)),
            "denylist": sorted(set(denylist)),
        }
    return normalized
