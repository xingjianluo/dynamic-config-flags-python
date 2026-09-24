"""非法配置拒绝原因可区分性测试。"""
import logging

import pytest

from app.schema import ConfigRejected, RejectReason, validate_config

logger = logging.getLogger("test")


@pytest.mark.parametrize(
    "raw,expected_reason",
    [
        ({"unknown_field": 1}, RejectReason.UNKNOWN_KEY),
        ({"max_connections": "100"}, RejectReason.TYPE_MISMATCH),
        ({"max_connections": True}, RejectReason.TYPE_MISMATCH),
        ({"debug": "false"}, RejectReason.TYPE_MISMATCH),
        ({"max_connections": 0}, RejectReason.OUT_OF_RANGE),
        ({"max_connections": 70000}, RejectReason.OUT_OF_RANGE),
        ({"request_timeout_ms": -1}, RejectReason.OUT_OF_RANGE),
        ({"flags": {"f": {"pct": 1}}}, RejectReason.UNKNOWN_KEY),
        ({"flags": {"f": {"enabled": "yes"}}}, RejectReason.TYPE_MISMATCH),
        ({"flags": {"f": {"percentage": 101}}}, RejectReason.OUT_OF_RANGE),
        ({"flags": {"f": {"percentage": -0.1}}}, RejectReason.OUT_OF_RANGE),
        ({"flags": {"f": {"allowlist": [1, 2]}}}, RejectReason.TYPE_MISMATCH),
    ],
)
def test_reject_reasons_are_distinguishable(raw, expected_reason):
    logger.info("输入非法配置=%s 期望原因=%s", raw, expected_reason.value)
    with pytest.raises(ConfigRejected) as exc_info:
        validate_config(raw)
    got = exc_info.value
    logger.info("实际拒绝原因=%s 明细=%s", got.reason.value, got.detail)
    assert got.reason is expected_reason


def test_valid_config_passes_and_unknown_not_silently_ignored():
    raw = {"max_connections": 50, "flags": {"f": {"enabled": True, "percentage": 10.5, "allowlist": ["u1"]}}}
    assert validate_config(raw)["max_connections"] == 50
    with pytest.raises(ConfigRejected) as exc_info:
        validate_config({"max_connections": 50, "typo_key": 1})
    assert exc_info.value.reason is RejectReason.UNKNOWN_KEY
