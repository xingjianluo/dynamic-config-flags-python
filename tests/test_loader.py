"""启动分层加载测试：默认值 < 文件 < 环境变量，非法配置启动即失败。"""
import json
import logging

import pytest

from app.loader import load_bootstrap
from app.schema import ConfigRejected, RejectReason

logger = logging.getLogger("test")


def test_bootstrap_priority(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"service_name": "from-file", "max_connections": 10}), encoding="utf-8")
    env = {"DYNCFG_MAX_CONNECTIONS": "42", "DYNCFG_DEBUG": "true"}
    logger.info("输入: 文件=%s 环境=%s", json.loads(cfg_file.read_text()), env)
    cfg, provenance = load_bootstrap(str(cfg_file), env)
    logger.info("加载结果=%s 来源=%s", cfg, provenance)
    assert cfg == {"service_name": "from-file", "max_connections": 42,
                   "request_timeout_ms": 3000, "debug": True}
    assert provenance["max_connections"] == "ENV"
    assert provenance["service_name"] == "FILE"
    assert provenance["request_timeout_ms"] == "DEFAULT"


def test_bootstrap_rejects_bad_env():
    with pytest.raises(ConfigRejected) as exc:
        load_bootstrap(None, {"DYNCFG_MAX_CONNECTIONS": "not-json"})
    assert exc.value.reason is RejectReason.TYPE_MISMATCH


def test_bootstrap_rejects_unknown_key_from_file(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"max_conn": 1}), encoding="utf-8")
    with pytest.raises(ConfigRejected) as exc:
        load_bootstrap(str(cfg_file), {})
    assert exc.value.reason is RejectReason.UNKNOWN_KEY
