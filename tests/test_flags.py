"""功能开关判定：确定性、名单命中与灰度比例。"""

import logging

import pytest

from app.errors import ConfigRejectedError, RejectionReason
from app.flags import bucket_of, evaluate_flag

logger = logging.getLogger(__name__)

FLAGS = {
    "new_checkout": {
        "enabled": True,
        "rollout": 50,
        "allowlist": ["alice"],
        "denylist": ["bob"],
    }
}


def test_same_subject_stable_decision():
    for subject in ["alice", "bob", "user-1", "user-2", "user-3"]:
        first = evaluate_flag("new_checkout", subject, FLAGS)
        for _ in range(20):
            again = evaluate_flag("new_checkout", subject, FLAGS)
            assert again == first, "配置未变时同一主体结论必须恒定"
        logger.info("主体=%s 结论=%s 依据=%s 分桶=%d", subject, first.enabled, first.reason, first.bucket)


def test_allowlist_and_denylist():
    allow = evaluate_flag("new_checkout", "alice", FLAGS)
    deny = evaluate_flag("new_checkout", "bob", FLAGS)
    logger.info("allowlist 判定: %s", allow)
    logger.info("denylist 判定: %s", deny)
    assert allow.enabled and allow.reason == "allowlist_hit"
    assert not deny.enabled and deny.reason == "denylist_hit"


def test_denylist_beats_allowlist():
    flags = {"f": {"enabled": True, "rollout": 100, "allowlist": ["x"], "denylist": ["x"]}}
    decision = evaluate_flag("f", "x", flags)
    logger.info("双名单冲突判定: %s", decision)
    assert not decision.enabled and decision.reason == "denylist_hit"


def test_rollout_matches_bucket_threshold():
    flags = {"f": {"enabled": True, "rollout": 30, "allowlist": [], "denylist": []}}
    for i in range(200):
        subject = f"user-{i}"
        decision = evaluate_flag("f", subject, flags)
        expected = bucket_of("f", subject) < 30
        assert decision.enabled is expected
    logger.info("rollout=30 时命中数: %d/200", sum(evaluate_flag('f', f'user-{i}', flags).enabled for i in range(200)))


def test_rollout_boundaries():
    subjects = [f"s{i}" for i in range(100)]
    off = {"f": {"enabled": True, "rollout": 0, "allowlist": [], "denylist": []}}
    on = {"f": {"enabled": True, "rollout": 100, "allowlist": [], "denylist": []}}
    assert not any(evaluate_flag("f", s, off).enabled for s in subjects)
    assert all(evaluate_flag("f", s, on).enabled for s in subjects)


def test_disabled_flag_overrides_rollout():
    flags = {"f": {"enabled": False, "rollout": 100, "allowlist": [], "denylist": []}}
    decision = evaluate_flag("f", "anyone", flags)
    logger.info("总开关关闭判定: %s", decision)
    assert not decision.enabled and decision.reason == "flag_disabled"


def test_unknown_flag_rejected():
    with pytest.raises(ConfigRejectedError) as exc_info:
        evaluate_flag("no_such_flag", "u", FLAGS)
    logger.info("未知开关拒绝: %s", exc_info.value)
    assert exc_info.value.reason is RejectionReason.UNKNOWN_KEY
