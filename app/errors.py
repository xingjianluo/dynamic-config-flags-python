"""错误类型与可区分的拒绝原因。"""

from __future__ import annotations

import enum


class RejectionReason(enum.Enum):
    """配置被拒绝的原因，需可区分、不得静默忽略。"""

    UNKNOWN_KEY = "unknown_key"
    TYPE_MISMATCH = "type_mismatch"
    OUT_OF_RANGE = "out_of_range"
    STALE_VERSION = "stale_version"
    UNKNOWN_VERSION = "unknown_version"


class ConfigRejectedError(Exception):
    """配置校验失败。携带可区分的拒绝原因与问题键列表。"""

    def __init__(self, reason: RejectionReason, keys: list[str], detail: str = "") -> None:
        self.reason = reason
        self.keys = list(keys)
        self.detail = detail
        super().__init__(f"{reason.value}: keys={self.keys} {detail}".strip())


class StaleVersionError(Exception):
    """基于过期版本的写入被拒绝。"""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"stale version: expected {expected}, current {actual}")


class UnknownVersionError(Exception):
    """回滚目标版本不存在。"""

    def __init__(self, version: int) -> None:
        self.version = version
        super().__init__(f"unknown version: {version}")
