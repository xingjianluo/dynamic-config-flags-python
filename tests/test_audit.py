"""变更审计：成功才记录、容量淘汰、回滚失败原因可区分、返回值独立。"""
import logging

import pytest

from app.schema import ConfigRejected, RejectReason
from app.store import ConfigStore

logger = logging.getLogger("test")


def test_successful_update_and_rollback_are_audited():
    store = ConfigStore({"max_connections": 10})
    store.update({"max_connections": 20, "debug": True}, store.version)
    store.rollback(0)
    records = store.get_audit_log()
    for r in records:
        logger.info("审计记录: version=%s kind=%s target=%s changes=%s",
                    r.version, r.kind, r.target_version, r.changes)
    assert len(records) == 2
    update_rec, rollback_rec = records
    # 更新记录：版本、时间、变更明细齐全
    assert update_rec.version == 1 and update_rec.kind == "update"
    assert update_rec.timestamp > 0
    assert update_rec.changes["max_connections"] == {
        "old": 10, "new": 20, "old_missing": False, "new_missing": False}
    assert update_rec.changes["debug"]["new"] is True
    assert update_rec.changes["debug"]["old_missing"] is True
    # 回滚记录：kind 与目标版本可辨识，明细与逆向变化一致
    assert rollback_rec.kind == "rollback" and rollback_rec.target_version == 0
    assert rollback_rec.version == 2
    assert rollback_rec.changes["max_connections"]["new"] == 10


def test_flag_changes_appear_in_audit_detail():
    store = ConfigStore({"flags": {"f1": {"enabled": True, "percentage": 10}}})
    store.update({"flags": {"f1": {"enabled": True, "percentage": 80},
                            "f2": {"enabled": False}}}, store.version)
    changes = store.get_audit_log()[0].changes
    logger.info("开关变更明细: %s", changes)
    assert changes["flags.f1"]["old"]["percentage"] == 10
    assert changes["flags.f1"]["new"]["percentage"] == 80
    assert changes["flags.f2"]["new_missing"] is False
    assert changes["flags.f2"]["old_missing"] is True


def test_rejected_writes_leave_no_audit_and_keep_config():
    store = ConfigStore({"max_connections": 10})
    before = store.get_config()
    for bad, reason in [
        ({"max_connections": 999999}, RejectReason.OUT_OF_RANGE),
        ({"nope": 1}, RejectReason.UNKNOWN_KEY),
        ({"max_connections": 10}, RejectReason.STALE_VERSION),  # expected_version 错位
    ]:
        logger.info("拒绝输入=%s 期望=%s", bad, reason.value)
        with pytest.raises(ConfigRejected):
            store.update(bad, expected_version=1 if reason is RejectReason.STALE_VERSION else store.version)
    assert store.get_audit_log() == []
    assert store.get_config() == before and store.version == 0
    # 失败的回滚同样不留审计
    with pytest.raises(ConfigRejected):
        store.rollback(99)
    assert store.get_audit_log() == []


def test_audit_capacity_evicts_oldest_and_bounds_rollback():
    store = ConfigStore({"max_connections": 1}, audit_capacity=3)
    for i in range(2, 7):  # 5 次成功变更，容量 3
        store.update({"max_connections": i}, store.version)
    records = store.get_audit_log()
    versions = [r.version for r in records]
    logger.info("容量=3, 5 次变更后审计版本=%s 可回滚版本=%s", versions, store.history_versions())
    assert versions == [3, 4, 5]  # 最旧的两条已被淘汰
    assert len(records) == 3

    # 已被淘汰的版本：明确报 VERSION_EVICTED，而不是悄悄回滚到别处
    with pytest.raises(ConfigRejected) as exc:
        store.rollback(1)
    assert exc.value.reason is RejectReason.VERSION_EVICTED
    logger.info("回滚已淘汰版本 1 -> %s", exc.value)
    assert store.version == 5 and store.get_config()["max_connections"] == 6

    # 从未存在过的版本：VERSION_NOT_FOUND，与淘汰可区分
    with pytest.raises(ConfigRejected) as exc:
        store.rollback(99)
    assert exc.value.reason is RejectReason.VERSION_NOT_FOUND

    # 仍在窗口内的旧版本：可以正常回滚
    new_version = store.rollback(3)
    assert store.get_config()["max_connections"] == 4
    assert store.get_audit_log()[-1].kind == "rollback"
    assert store.get_audit_log()[-1].target_version == 3
    logger.info("窗口内回滚版本 3 成功，新版本=%s", new_version)


def test_audit_log_return_value_is_independent():
    store = ConfigStore({"max_connections": 1})
    store.update({"max_connections": 2}, store.version)
    records = store.get_audit_log()
    records[0].changes["max_connections"]["new"] = 99999
    records.clear()
    fresh = store.get_audit_log()
    logger.info("篡改返回值后重新读取: %s", fresh)
    assert len(fresh) == 1
    assert fresh[0].changes["max_connections"]["new"] == 2
