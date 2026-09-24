"""功能开关：按主体判定，支持比例灰度与名单命中，结论稳定可复现。

判定顺序（自上而下，第一条命中即结论）：
1. 规则 enabled=false          -> 拒绝，依据 disabled
2. 主体命中 allowlist          -> 放行，依据 allowlist
3. 按 (开关名, 主体) 稳定哈希取桶，桶位 < percentage -> 放行，依据 percentage
   桶位算法：SHA-256("flag:subject") 取十六进制前 8 位对 10000 取模，映射到 [0, 100)，
   粒度 0.01%。哈希与时间、进程、随机数无关，因此同一主体在同一配置下结论恒定。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FlagRule:
    """单个开关规则。"""

    name: str
    enabled: bool = True
    percentage: float = 0.0  # 0~100 灰度比例
    allowlist: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class FlagDecision:
    """判定结论与依据。"""

    flag: str
    subject: str
    allowed: bool
    basis: str  # 判定依据说明，用于日志与测试断言
    bucket: float | None = None  # 命中灰度时的桶位，便于解释


def bucket_of(flag: str, subject: str) -> float:
    """将 (开关, 主体) 稳定映射到 [0, 100) 桶位，粒度 0.01。"""
    digest = hashlib.sha256(f"{flag}:{subject}".encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 10000) / 100.0


def evaluate(rule: FlagRule, subject: str) -> FlagDecision:
    """对单个主体判定开关，结论确定且可解释。"""
    if not rule.enabled:
        return FlagDecision(rule.name, subject, False, "开关已关闭(enabled=false)")
    if subject in rule.allowlist:
        return FlagDecision(rule.name, subject, True, "命中白名单(allowlist)")
    bucket = bucket_of(rule.name, subject)
    if rule.percentage <= 0:
        return FlagDecision(
            rule.name, subject, False, f"灰度比例为0，桶位={bucket:.2f}", bucket
        )
    if rule.percentage >= 100:
        return FlagDecision(
            rule.name, subject, True, "灰度比例为100全量放开", bucket
        )
    allowed = bucket < rule.percentage
    basis = (
        f"按哈希桶位灰度：桶位={bucket:.2f} < 比例={rule.percentage:g}%"
        if allowed
        else f"按哈希桶位灰度：桶位={bucket:.2f} >= 比例={rule.percentage:g}%"
    )
    return FlagDecision(rule.name, subject, allowed, basis, bucket)


def flags_from_config(config: dict[str, Any]) -> dict[str, FlagRule]:
    """从生效配置解析开关规则表；配置不含 flags 时返回空表。"""
    rules: dict[str, FlagRule] = {}
    for name, raw in config.get("flags", {}).items():
        rules[name] = FlagRule(
            name=name,
            enabled=bool(raw.get("enabled", True)),
            percentage=float(raw.get("percentage", 0.0)),
            allowlist=frozenset(raw.get("allowlist", [])),
        )
    return rules
