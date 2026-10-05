"""来源追溯：值与来源必须严格对应，热更新/回滚后立即同步，开关来源可解释。"""
import logging

from app.store import ConfigStore

logger = logging.getLogger("test")


def test_bootstrap_provenance_per_key_and_per_flag():
    initial = {
        "service_name": "svc",
        "max_connections": 100,
        "flags": {"new-ui": {"enabled": True, "percentage": 10}},
    }
    prov = {"service_name": "DEFAULT", "max_connections": "FILE", "flags": "ENV"}
    logger.info("输入初始配置=%s 来源=%s", initial, prov)
    store = ConfigStore(initial, initial_provenance=prov)
    got = store.get_provenance()
    logger.info("生效来源=%s", got)
    assert got == {
        "service_name": "DEFAULT",
        "max_connections": "FILE",
        "flags": "ENV",
        "flags.new-ui": "ENV",  # flags 整表来源细化到每个开关
    }
    # 来源键集合与生效键一一对应（flags 展开到开关）
    view = store.get_effective()
    assert view.provenance["max_connections"] == "FILE"
    assert set(view.provenance) == {"service_name", "max_connections", "flags", "flags.new-ui"}


def test_provenance_follows_hot_update_immediately():
    store = ConfigStore(
        {"max_connections": 100, "debug": False},
        {"max_connections": "FILE", "debug": "DEFAULT"},
    )
    assert store.get_provenance()["max_connections"] == "FILE"

    v1 = store.update(
        {"max_connections": 200, "debug": True, "flags": {"f": {"percentage": 50}}}, 0
    )
    prov = store.get_provenance()
    cfg = store.get_config()
    logger.info("热更新后 v%s: 配置=%s 来源=%s", v1, cfg, prov)
    # 值变了，来源必须马上同步到 RUNTIME，不能停在 FILE/DEFAULT
    assert cfg["max_connections"] == 200 and prov["max_connections"] == "RUNTIME"
    assert cfg["debug"] is True and prov["debug"] == "RUNTIME"
    assert prov["flags"] == "RUNTIME" and prov["flags.f"] == "RUNTIME"

    # get_effective 一次性取到的值与来源来自同一快照，天然配套
    view = store.get_effective()
    assert view.version == v1
    assert view.config["max_connections"] == 200 and view.provenance["max_connections"] == "RUNTIME"
    assert view.provenance["flags.f"] == "RUNTIME"


def test_provenance_restored_on_rollback():
    store = ConfigStore(
        {"max_connections": 100}, {"max_connections": "FILE"}
    )
    v0 = store.version
    store.update({"max_connections": 300}, v0)
    assert store.get_provenance()["max_connections"] == "RUNTIME"
    v2 = store.rollback(v0)
    cfg, prov = store.get_config(), store.get_provenance()
    logger.info("回滚后 v%s: 配置=%s 来源=%s", v2, cfg, prov)
    assert cfg["max_connections"] == 100
    # 值回退到 v0，来源也必须一起回退到 v0 的 FILE，而非停在 RUNTIME
    assert prov["max_connections"] == "FILE"


def test_provenance_and_flag_decision_stay_consistent_across_updates():
    store = ConfigStore({"flags": {"f": {"enabled": True, "percentage": 0}}},
                        initial_provenance={"flags": "FILE"})
    v = store.version
    # 热更新关闭开关，来源与判定同步
    v = store.update({"flags": {"f": {"enabled": False, "percentage": 0}}}, v)
    snap = store.snapshot()
    logger.info("v%s 来源 flags=%s 判定=%s", v, snap.provenance.get("flags.f"),
                store.check_flag("f", "u").basis)
    assert snap.provenance["flags.f"] == "RUNTIME"
    assert store.check_flag("f", "u").allowed is False
    # 再热更新 100% 放开，来源仍与值一致
    v = store.update({"flags": {"f": {"enabled": True, "percentage": 100}}}, v)
    assert store.snapshot().provenance["flags.f"] == "RUNTIME"
    assert store.check_flag("f", "u").allowed is True
