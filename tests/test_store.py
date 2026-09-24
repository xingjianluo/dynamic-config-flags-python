"""版本管理：乐观锁、失败不破坏当前配置、回滚一致性测试。"""
import logging

import pytest

from app.schema import ConfigRejected, RejectReason
from app.store import ConfigStore

logger = logging.getLogger("test")


def test_stale_version_write_is_rejected():
    store = ConfigStore()
    store.update({"max_connections": 10}, 0)
    logger.info("当前版本=1, 尝试基于过期版本 0 写入")
    with pytest.raises(ConfigRejected) as exc:
        store.update({"max_connections": 20}, 0)
    assert exc.value.reason is RejectReason.STALE_VERSION
    assert store.version == 1 and store.get_config()["max_connections"] == 10


def test_invalid_update_keeps_previous_good_config():
    store = ConfigStore({"max_connections": 10})
    assert store.update({"max_connections": 20}, store.version) == 1
    good = store.get_config()
    version_before = store.version
    for bad, reason in [
        ({"max_connections": 999999}, RejectReason.OUT_OF_RANGE),
        ({"nope": 1}, RejectReason.UNKNOWN_KEY),
        ({"max_connections": "x"}, RejectReason.TYPE_MISMATCH),
    ]:
        logger.info("输入非法配置=%s 期望拒绝=%s 当前版本=%s", bad, reason.value, store.version)
        with pytest.raises(ConfigRejected) as exc:
            store.update(bad, store.version)
        assert exc.value.reason is reason
        assert store.version == version_before
        assert store.get_config() == good  # 沿用上一份可用配置


def test_rollback_restores_historical_decisions():
    store = ConfigStore({"flags": {"f": {"enabled": True, "percentage": 0}}})
    v1 = store.version
    d1_off = store.check_flag("f", "user-7")
    logger.info("v%s 判定: subject=user-7 结论=%s 依据=%s", v1, d1_off.allowed, d1_off.basis)

    v2 = store.update({"flags": {"f": {"enabled": True, "percentage": 100}}}, v1)
    assert store.check_flag("f", "user-7").allowed is True

    v3 = store.rollback(v1)
    logger.info("回滚 v%s -> 新版本 v%s", v1, v3)
    assert v3 == 2
    d_after = store.check_flag("f", "user-7")
    logger.info("回滚后判定: 结论=%s 依据=%s", d_after.allowed, d_after.basis)
    assert d_after == d1_off  # 判定与目标历史版本完全一致
    assert store.get_config() == {"flags": {"f": {"enabled": True, "percentage": 0}}}
    assert store.history_versions() == [0, 1, 2]


def test_rollback_unknown_version_rejected():
    store = ConfigStore()
    with pytest.raises(ConfigRejected) as exc:
        store.rollback(404)
    assert exc.value.reason is RejectReason.VERSION_NOT_FOUND


def test_unknown_flag_fails_closed():
    store = ConfigStore()
    d = store.check_flag("missing", "u1")
    logger.info("未知开关判定: %s", d.basis)
    assert d.allowed is False and "fail-closed" in d.basis
