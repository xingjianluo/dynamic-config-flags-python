"""功能开关判定：按比例灰度与按名单命中。

确定性分桶：bucket = sha256(f"{flag}:{subject}") 取模 100，
同一主体在配置未变时结论恒定。判定顺序固定且可解释：
denylist > allowlist > 总开关 > 灰度比例。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.errors import ConfigRejectedError, RejectionReason


@dataclass(frozen=True)
class FlagDecision:
    """一次开关判定的结论与依据。"""

    flag: str
    subject: str
    enabled: bool
    reason: str
    bucket: int | None


def bucket_of(flag: str, subject: str) -> int:
    """确定性分桶，返回 0..99。"""
    digest = hashlib.sha256(f"{flag}:{subject}".encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


def evaluate_flag(flag: str, subject: str, flags_config: dict[str, Any]) -> FlagDecision:
    """对指定主体判定开关。未知开关拒绝而非静默关闭。"""
    rule = flags_config.get(flag)
    if rule is None:
        raise ConfigRejectedError(RejectionReason.UNKNOWN_KEY, [flag], "unknown flag")
    bucket = bucket_of(flag, subject)
    if subject in rule["denylist"]:
        return FlagDecision(flag, subject, False, "denylist_hit", bucket)
    if subject in rule["allowlist"]:
        return FlagDecision(flag, subject, True, "allowlist_hit", bucket)
    if not rule["enabled"]:
        return FlagDecision(flag, subject, False, "flag_disabled", bucket)
    if bucket < rule["rollout"]:
        return FlagDecision(flag, subject, True, "rollout_hit", bucket)
    return FlagDecision(flag, subject, False, "rollout_miss", bucket)
