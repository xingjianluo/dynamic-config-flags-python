"""只读预演：不生效/不占版本/不写审计，摘要准确，并发隔离，返回值独立。"""
import logging
import threading

from app.store import ConfigStore

logger = logging.getLogger("test")


def test_dry_run_accepted_gives_summary_without_side_effects():
    store = ConfigStore({"max_connections": 10, "flags": {"f1": {"enabled": True, "percentage": 5}}})
    candidate = {"max_connections": 20, "flags": {"f1": {"enabled": True, "percentage": 50},
                                                  "f2": {"enabled": False}}}
    result = store.dry_run(candidate, expected_version=0)
    logger.info("预演接受: 基于版本=%s 变更=%s 来源变化=%s",
                result.based_on_version, result.changes, result.provenance_changes)
    assert result.accepted is True and result.reason is None
    assert result.based_on_version == 0
    # 变更摘要：普通键与开关逐项都在
    assert result.changes["max_connections"]["old"] == 10
    assert result.changes["max_connections"]["new"] == 20
    assert result.changes["flags.f1"]["new"]["percentage"] == 50
    assert result.changes["flags.f2"]["old_missing"] is True
    # 来源摘要：10 -> RUNTIME，f2 新增来源为 RUNTIME
    assert result.provenance_changes["max_connections"] == {"old": "DEFAULT", "new": "RUNTIME"}
    assert result.provenance_changes["flags.f2"] == {"old": None, "new": "RUNTIME"}
    # 只读性：版本、配置、来源、审计全部不变
    assert store.version == 0
    assert store.get_config()["max_connections"] == 10
    assert store.get_provenance()["max_connections"] == "DEFAULT"
    assert store.get_audit_log() == []


def test_dry_run_rejected_gives_distinguishable_reasons():
    store = ConfigStore({"max_connections": 10})
    cases = [
        ({"nope": 1}, 0, "unknown_key"),
        ({"max_connections": "x"}, 0, "type_mismatch"),
        ({"max_connections": 999999}, 0, "out_of_range"),
        ({"max_connections": 20}, 7, "stale_version"),
    ]
    for candidate, expected_version, reason in cases:
        result = store.dry_run(candidate, expected_version)
        logger.info("预演拒绝: 输入=%s expected=%s -> reason=%s detail=%s",
                    candidate, expected_version, result.reason, result.detail)
        assert result.accepted is False
        assert result.reason == reason
        assert result.changes is None
    assert store.version == 0 and store.get_audit_log() == []


def test_dry_run_does_not_consume_version():
    store = ConfigStore({"max_connections": 10})
    result = store.dry_run({"max_connections": 20}, expected_version=0)
    assert result.accepted is True
    # 预演不占用版本号：同一 expected_version 的真实写入仍然成功
    assert store.update({"max_connections": 20}, expected_version=0) == 1
    assert store.get_config()["max_connections"] == 20
    # 真实写入产生一条审计，预演没有额外产生
    assert len(store.get_audit_log()) == 1


def test_dry_run_concurrent_with_real_updates_is_isolated():
    store = ConfigStore({"max_connections": 1})
    errors: list[Exception] = []
    stop = threading.Event()

    def writer():
        for i in range(2, 102):
            try:
                store.update({"max_connections": i}, store.version)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
        stop.set()

    def rehearser():
        while not stop.is_set():
            try:
                version = store.version
                result = store.dry_run({"max_connections": 777}, version)
                # 预演结果必须自洽：基于的版本与结论对应某一完整快照
                if result.accepted:
                    assert result.based_on_version == version
                    assert result.changes["max_connections"]["new"] == 777
                    old = result.changes["max_connections"]["old"]
                    assert isinstance(old, int) and 1 <= old <= 101  # 只见完整版本，无中间态
                else:
                    assert result.reason == "stale_version"
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=rehearser) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logger.info("并发结束: 版本=%s 审计条数=%s 错误=%s",
                store.version, len(store.get_audit_log()), errors)
    assert not errors
    assert store.version == 100 and store.get_config()["max_connections"] == 101
    assert len(store.get_audit_log()) == 100  # 只有真实写入留审计


def test_dry_run_result_is_independent_of_store():
    store = ConfigStore({"max_connections": 10})
    result = store.dry_run({"max_connections": 20}, expected_version=0)
    result.changes["max_connections"]["new"] = -1
    result.provenance_changes["max_connections"]["new"] = "HACKED"
    again = store.dry_run({"max_connections": 20}, expected_version=0)
    logger.info("篡改返回值后再次预演: %s %s", again.changes, again.provenance_changes)
    assert again.changes["max_connections"]["new"] == 20
    assert again.provenance_changes["max_connections"]["new"] == "RUNTIME"
    assert store.get_config()["max_connections"] == 10
