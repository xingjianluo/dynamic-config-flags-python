"""变更审计：成功变更留痕、失败写入不留痕、容量淘汰、回滚失败路径可区分、返回值独立。"""
import logging

import pytest

from app.schema import ConfigRejected, RejectReason
from app.store import ConfigStore

logger = logging.getLogger("test")


def test_successful_update_and_rollback_leave_audit_records():
    store = ConfigStore({"max_connections": 100}, clock=lambda: "T")
    v1 = store.update({"max_connections": 200}, 0)
    v2 = store.rollback(0)
    records = store.audit_records()
    logger.info("审计记录=%s", [(r.sequence, r.timestamp, r.kind, r.from_version,
                                r.version, r.target_version) for r in records])
    assert len(records) == 2
    r_upd, r_rb = records
    assert r_upd.kind == "update" and r_upd.version == v1 and r_upd.from_version == 0
    assert r_upd.changes["keys"]["changed"]["max_connections"] == {"old": 100, "new": 200}
    assert r_rb.kind == "rollback" and r_rb.version == v2 and r_rb.target_version == 0
    # 序号单调、时间存在
    assert r_upd.sequence == 1 and r_rb.sequence == 2
    assert r_upd.timestamp and r_rb.timestamp


def test_rejected_or_failed_writes_leave_no_audit_and_keep_effective_config():
    store = ConfigStore({"max_connections": 100}, history_capacity=64, clock=lambda: "T")
    good_view = store.get_effective()
    attempts = [
        ({"unknown_key": 1}, 0, RejectReason.UNKNOWN_KEY),
        ({"max_connections": 999999}, 0, RejectReason.OUT_OF_RANGE),
        ({"max_connections": "x"}, 0, RejectReason.TYPE_MISMATCH),
        ({"max_connections": 200}, 99, RejectReason.STALE_VERSION),
    ]
    for raw, ver, reason in attempts:
        logger.info("输入=%s expected=%s 期望拒绝=%s", raw, ver, reason.value)
        with pytest.raises(ConfigRejected) as exc:
            store.update(raw, ver)
        assert exc.value.reason is reason
        # 失败回滚也不留痕：回滚不存在的版本
    with pytest.raises(ConfigRejected) as exc:
        store.rollback(404)
    assert exc.value.reason is RejectReason.VERSION_NOT_FOUND
    with pytest.raises(ConfigRejected) as exc:
        store.rollback(-5)
    assert exc.value.reason is RejectReason.VERSION_NOT_FOUND

    assert store.audit_records() == []  # 没有任何成功变更 -> 无记录
    view = store.get_effective()
    assert view.version == 0 and view.config == good_view.config  # 生效配置不受影响


def test_audit_capacity_evicts_oldest():
    store = ConfigStore({"max_connections": 10}, audit_capacity=3, clock=lambda: "T")
    v = 0
    for i in range(1, 6):
        v = store.update({"max_connections": 10 + i}, v)
    records = store.audit_records()
    versions = [r.version for r in records]
    logger.info("容量=3，5 次写入后保留审计版本=%s", versions)
    assert len(records) == 3 and versions == [3, 4, 5]  # 最旧的 1、2 被淘汰
    assert records[0].sequence == 3  # 序号不回卷、不复用
    assert records[-1].changes["keys"]["changed"]["max_connections"]["new"] == 15


def test_history_eviction_rollback_evicted_distinct_from_not_found():
    store = ConfigStore({"max_connections": 1}, history_capacity=3, clock=lambda: "T")
    v = 0
    for i in range(2, 5):
        v = store.update({"max_connections": i}, v)
    logger.info("历史版本=%s", store.history_versions())
    assert store.history_versions() == [1, 2, 3]  # v0 已被淘汰

    # 回滚到被淘汰的 v0：明确 VERSION_EVICTED，且不静默回滚到别处
    with pytest.raises(ConfigRejected) as exc:
        store.rollback(0)
    logger.info("回滚被淘汰版本: reason=%s detail=%s", exc.value.reason.value, exc.value.detail)
    assert exc.value.reason is RejectReason.VERSION_EVICTED
    assert store.version == 3 and store.get_config()["max_connections"] == 4

    # 从未存在过的未来版本：VERSION_NOT_FOUND，两者可区分
    with pytest.raises(ConfigRejected) as exc2:
        store.rollback(99)
    assert exc2.value.reason is RejectReason.VERSION_NOT_FOUND

    # 失败的回滚尝试不产生审计
    assert [r.version for r in store.audit_records()] == [1, 2, 3]

    # 回滚仍保留的 v1：成功并产生审计
    v4 = store.rollback(1)
    assert store.get_config()["max_connections"] == 2
    last = store.audit_records()[-1]
    assert last.kind == "rollback" and last.version == v4 and last.target_version == 1


def test_audit_return_values_are_independent_copies():
    store = ConfigStore({"max_connections": 100})
    store.update({"max_connections": 200}, 0)
    recs = store.audit_records()
    recs[0].changes["injected"] = {"evil": True}
    recs.append("tampered")
    again = store.audit_records()
    assert "injected" not in again[0].changes
    assert len(again) == 1 and isinstance(again[0].changes, dict)
