"""版本化存储：过期版本拒绝、失败保护、回滚一致性。"""

import logging

import pytest

from app.errors import ConfigRejectedError, StaleVersionError, UnknownVersionError
from app.flags import evaluate_flag
from app.store import ConfigStore

logger = logging.getLogger(__name__)

BASE_CONFIG = {"service.port": 8080, "service.log_level": "INFO"}
BASE_FLAGS = {
    "dark_mode": {"enabled": False, "rollout": 0, "allowlist": [], "denylist": []}
}


def make_store() -> ConfigStore:
    return ConfigStore(dict(BASE_CONFIG), {k: dict(v) for k, v in BASE_FLAGS.items()})


def test_update_requires_current_version():
    store = make_store()
    snap = store.update({"service.port": 9000}, expected_version=1)
    logger.info("v1->v2 输入: port=9000, 生效: %s", snap.config)
    assert snap.version == 2 and snap.config["service.port"] == 9000
    with pytest.raises(StaleVersionError) as exc_info:
        store.update({"service.port": 1}, expected_version=1)
    logger.info("过期版本拒绝: %s", exc_info.value)
    assert exc_info.value.expected == 1 and exc_info.value.actual == 2
    assert store.snapshot().config["service.port"] == 9000


def test_failed_update_preserves_current_config():
    store = make_store()
    before = store.snapshot()
    for bad in ({"unknown.key": 1}, {"service.port": "x"}, {"service.port": 0}):
        with pytest.raises(ConfigRejectedError) as exc_info:
            store.update(bad, expected_version=before.version)
        logger.info("非法输入 %s 被拒绝: %s", bad, exc_info.value.reason.value)
    after = store.snapshot()
    assert after.version == before.version
    assert after.config == before.config, "失败不得破坏当前生效配置"


def test_rollback_restores_historical_decisions():
    store = make_store()
    v1 = store.snapshot()
    d1 = evaluate_flag("dark_mode", "user-1", v1.flags)
    store.update(
        flags={"dark_mode": {"enabled": True, "rollout": 100, "allowlist": [], "denylist": []}},
        expected_version=1,
    )
    d2 = evaluate_flag("dark_mode", "user-1", store.snapshot().flags)
    logger.info("v1 判定: %s; v2 判定: %s", d1, d2)
    assert not d1.enabled and d2.enabled
    rolled = store.rollback(1)
    d3 = evaluate_flag("dark_mode", "user-1", rolled.flags)
    logger.info("回滚后 v%d 判定: %s", rolled.version, d3)
    assert rolled.version == 3, "回滚生成新版本号"
    assert d3 == d1, "回滚后的判定结果必须与历史版本一致"
    assert rolled.config["service.port"] == v1.config["service.port"]


def test_rollback_unknown_version_rejected():
    store = make_store()
    with pytest.raises(UnknownVersionError):
        store.rollback(42)
    assert store.snapshot().version == 1


def test_snapshot_is_immutable_for_caller():
    store = make_store()
    snap = store.snapshot()
    snap.config["service.port"] = 1
    snap.flags["dark_mode"]["enabled"] = True
    again = store.snapshot()
    assert again.config["service.port"] == 8080
    assert again.flags["dark_mode"]["enabled"] is False
