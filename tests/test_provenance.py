"""来源追溯：来源说明与生效值一致，热更新/回滚后同步，返回值独立。"""
import json
import logging

from app.loader import load_bootstrap
from app.store import ConfigStore

logger = logging.getLogger("test")


def _make_store(tmp_path):
    """三层来源：DEFAULT < FILE < ENV，构造带来源的存储。"""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "max_connections": 200,
        "request_timeout_ms": 5000,
        "flags": {"file-flag": {"enabled": True, "percentage": 50}},
    }), encoding="utf-8")
    environ = {"DYNCFG_MAX_CONNECTIONS": "300"}
    merged, provenance = load_bootstrap(str(cfg_file), environ)
    logger.info("分层加载结果: 配置=%s 来源=%s", merged, provenance)
    return ConfigStore(merged, provenance), merged


def test_bootstrap_provenance_matches_effective_values(tmp_path):
    store, merged = _make_store(tmp_path)
    prov = store.get_provenance()
    config = store.get_config()
    logger.info("生效配置=%s 来源=%s", config, prov)
    # 每个生效键都有来源，且来源层级与值对得上
    assert config == merged
    assert prov["max_connections"] == "ENV" and config["max_connections"] == 300
    assert prov["request_timeout_ms"] == "FILE" and config["request_timeout_ms"] == 5000
    assert prov["service_name"] == "DEFAULT"
    assert prov["debug"] == "DEFAULT"
    # 开关逐项来源与 flags 整表来源一致
    assert prov["flags"] == "FILE"
    assert prov["flags.file-flag"] == "FILE"


def test_provenance_syncs_after_hot_update(tmp_path):
    store, _ = _make_store(tmp_path)
    new_config = {
        "max_connections": 999,
        "flags": {"rt-flag": {"enabled": True, "percentage": 10, "allowlist": ["u1"]}},
    }
    logger.info("热更新输入=%s 基于版本=%s", new_config, store.version)
    store.update(new_config, store.version)
    prov, config = store.get_provenance(), store.get_config()
    logger.info("热更新后: 配置=%s 来源=%s", config, prov)
    # 值与来源同步切换：生效键全部来自 RUNTIME，不残留旧的 ENV/FILE 标记
    assert config["max_connections"] == 999 and prov["max_connections"] == "RUNTIME"
    assert "request_timeout_ms" not in config and "request_timeout_ms" not in prov
    assert "flags.file-flag" not in prov  # 旧开关的来源标记同步消失
    assert prov["flags.rt-flag"] == "RUNTIME" and prov["flags"] == "RUNTIME"


def test_provenance_syncs_after_rollback(tmp_path):
    store, merged = _make_store(tmp_path)
    store.update({"debug": True}, store.version)  # 版本 1：全部 RUNTIME
    assert store.get_provenance()["debug"] == "RUNTIME"
    logger.info("回滚到版本 0（分层加载的初始配置）")
    store.rollback(0)
    prov, config = store.get_provenance(), store.get_config()
    logger.info("回滚后: 配置=%s 来源=%s", config, prov)
    # 来源与值一起回到目标版本，而不是停留在 RUNTIME
    assert config == merged
    assert prov["max_connections"] == "ENV" and config["max_connections"] == 300
    assert prov["flags.file-flag"] == "FILE"
    # debug 回到 DEFAULT 层的 False，而不是热更新里的 RUNTIME=True
    assert prov["debug"] == "DEFAULT" and config["debug"] is False


def test_provenance_return_value_is_independent(tmp_path):
    store, _ = _make_store(tmp_path)
    prov = store.get_provenance()
    prov["max_connections"] = "HACKED"
    prov["flags.file-flag"] = "HACKED"
    logger.info("篡改返回值后重新读取: %s", store.get_provenance())
    assert store.get_provenance()["max_connections"] == "ENV"
    assert store.get_provenance()["flags.file-flag"] == "FILE"
