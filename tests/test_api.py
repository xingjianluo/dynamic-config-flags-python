"""HTTP 接口：配置查询/更新/回滚与开关判定。"""

import logging

import pytest
from fastapi.testclient import TestClient

from app.service import create_app, create_store

logger = logging.getLogger(__name__)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"service.port": 9000, "service.log_level": "WARNING"}')
    monkeypatch.setenv("APP_SERVICE_PORT", "7000")  # ENV 优先于 FILE
    store = create_store(str(config_file))
    logger.info("初始快照: %s", store.snapshot())
    return TestClient(create_app(store))


def test_layered_priority_visible_via_api(client):
    body = client.get("/config").json()
    logger.info("生效配置: %s", body["config"])
    assert body["config"]["service.port"] == 7000  # ENV 覆盖 FILE
    assert body["config"]["service.log_level"] == "WARNING"  # FILE 覆盖 DEFAULTS
    assert body["config"]["service.name"] == "dynamic-config-flags"  # DEFAULTS


def test_update_stale_and_invalid_via_api(client):
    ok = client.post("/config", json={"config": {"limits.timeout_ms": 5000}, "expected_version": 1})
    assert ok.status_code == 200 and ok.json()["version"] == 2

    stale = client.post("/config", json={"config": {"limits.timeout_ms": 1}, "expected_version": 1})
    logger.info("过期版本响应: %s", stale.json())
    assert stale.status_code == 409

    invalid = client.post("/config", json={"config": {"limits.timeout_ms": 0}, "expected_version": 2})
    logger.info("越界响应: %s", invalid.json())
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["reason"] == "out_of_range"
    # 失败不破坏当前配置
    assert client.get("/config").json()["config"]["limits.timeout_ms"] == 5000


def test_flag_decision_and_rollback_via_api(client):
    update = client.post(
        "/config",
        json={
            "flags": {"dark_mode": {"enabled": True, "rollout": 100, "allowlist": [], "denylist": []}},
            "expected_version": 1,
        },
    )
    assert update.status_code == 200
    d2 = client.get("/flags/dark_mode", params={"subject": "user-1"}).json()
    logger.info("v2 判定: %s", d2)
    assert d2["enabled"] is True and d2["reason"] == "rollout_hit"

    rb = client.post("/config/rollback", json={"version": 1})
    assert rb.status_code == 200 and rb.json()["version"] == 3
    d3 = client.get("/flags/dark_mode", params={"subject": "user-1"}).json()
    logger.info("回滚后判定: %s", d3)
    assert d3["enabled"] is False and d3["reason"] == "flag_disabled"

    missing = client.post("/config/rollback", json={"version": 999})
    assert missing.status_code == 404


def test_unknown_flag_via_api(client):
    resp = client.get("/flags/nope", params={"subject": "u"})
    logger.info("未知开关响应: %s", resp.json())
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "unknown_key"
