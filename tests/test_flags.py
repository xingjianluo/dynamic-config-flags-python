"""功能开关：白名单、灰度比例、确定性判定测试。"""
import logging

from app.flags import FlagRule, bucket_of, evaluate, flags_from_config

logger = logging.getLogger("test")


def test_disabled_flag_denies_everyone():
    rule = FlagRule("f", enabled=False, percentage=100, allowlist=frozenset({"vip"}))
    for subject in ["vip", "u1", "u2"]:
        d = evaluate(rule, subject)
        logger.info("输入: flag=f subject=%s 规则=disabled 结论=%s 依据=%s", subject, d.allowed, d.basis)
        assert d.allowed is False


def test_allowlist_takes_precedence_over_zero_percentage():
    rule = FlagRule("f", enabled=True, percentage=0, allowlist=frozenset({"vip"}))
    d = evaluate(rule, "vip")
    logger.info("输入: subject=vip percentage=0 allowlist=['vip'] 结论=%s 依据=%s", d.allowed, d.basis)
    assert d.allowed is True and "白名单" in d.basis
    assert evaluate(rule, "nobody").allowed is False


def test_percentage_0_and_100_boundaries():
    rule0 = FlagRule("f", True, percentage=0)
    rule100 = FlagRule("f", True, percentage=100)
    assert all(evaluate(rule0, f"u{i}").allowed is False for i in range(50))
    assert all(evaluate(rule100, f"u{i}").allowed is True for i in range(50))


def test_deterministic_for_same_subject():
    rule = flags_from_config({"flags": {"new-ui": {"enabled": True, "percentage": 30, "allowlist": []}}})["new-ui"]
    first = evaluate(rule, "user-42")
    logger.info("输入: flag=new-ui subject=user-42 percentage=30 桶位=%s 结论=%s", first.bucket, first.allowed)
    for _ in range(100):
        assert evaluate(rule, "user-42") == first


def test_percentage_distribution_roughly_matches_buckets():
    rule = FlagRule("f", True, percentage=50)
    subjects = [f"user-{i}" for i in range(2000)]
    allowed = [evaluate(rule, s).allowed for s in subjects]
    ratio = sum(allowed) / len(subjects)
    logger.info("输入: 2000 个主体 percentage=50 实际放行比例=%.3f", ratio)
    assert 0.45 < ratio < 0.55


def test_bucket_is_in_range_and_stable():
    b = bucket_of("f", "user-1")
    assert 0.0 <= b < 100.0
    assert bucket_of("f", "user-1") == b
