"""HTTP 端到端：读取、更新、过期拒绝、回滚、开关判定。"""
import logging

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

logger = logging.getLogger("test")
client = TestClient(main.app)


def test_full_lifecycle():
    base = client.get("/config").json()
    logger.info("初始: version=%s config=%s", base["version"], base["config"])

    payload = {"flags": {"new-ui": {"enabled": True, "percentage": 0, "allowlist": ["vip"]}}}
    logger.info("更新输入=%s expected_version=%s", payload, base["version"])
    resp = client.post("/config", json={"expected_version": base["version"], "config": payload})
    assert resp.status_code == 200
    v1 = resp.json()["version"]

    # 白名单命中
    d = client.get("/flags/new-ui", params={"subject": "vip"}).json()
    logger.info("判定依据=%s", d)
    assert d["allowed"] is True

    # 过期版本被拒
    stale = client.post("/config", json={"expected_version": base["version"], "config": payload})
    logger.info("过期写入响应=%s", stale.json())
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "stale_version"

    # 非法配置被拒，原因可区分
    bad = client.post("/config", json={"expected_version": v1, "config": {"flags": {"new-ui": {"percentage": 300}}}})
    assert bad.status_code == 409 and bad.json()["detail"]["reason"] == "out_of_range"

    # 回滚到初始版本
    rb = client.post("/config/rollback", json={"target_version": base["version"]})
    assert rb.status_code == 200
    assert client.get("/flags/new-ui", params={"subject": "vip"}).json()["allowed"] is False
