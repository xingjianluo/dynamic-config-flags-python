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


def test_provenance_preview_and_audit_endpoints():
    # 来源：热更新后 /config 的 provenance 必须同步为 RUNTIME
    base = client.get("/config").json()
    payload = {"max_connections": 250, "flags": {"beta": {"enabled": True, "percentage": 5}}}
    upd = client.post("/config", json={"expected_version": base["version"], "config": payload})
    assert upd.status_code == 200
    cur = client.get("/config").json()
    logger.info("热更新后 /config: config=%s provenance=%s", cur["config"], cur["provenance"])
    assert cur["config"]["max_connections"] == 250
    assert cur["provenance"]["max_connections"] == "RUNTIME"
    assert cur["provenance"]["flags"] == "RUNTIME" and cur["provenance"]["flags.beta"] == "RUNTIME"

    # 预演：可接受时给出变化摘要与来源预测，且不改变版本/不产生审计
    preview = client.post("/config/preview", json={
        "expected_version": cur["version"],
        "config": {"max_connections": 350, "flags": {"beta": {"enabled": False}}},
    }).json()
    logger.info("预演响应=%s", preview)
    assert preview["accepted"] is True
    assert preview["base_version"] == cur["version"]
    assert preview["would_be_version"] == cur["version"] + 1
    assert preview["changes"]["keys"]["changed"]["max_connections"] == {"old": 250, "new": 350}
    assert preview["changes"]["flags"]["changed"]["beta"]["enabled"] == {
        "old": True, "new": False, "present_old": True, "present_new": True,
    }
    assert preview["provenance_after"]["flags.beta"] == "RUNTIME"
    still = client.get("/config").json()
    assert still["version"] == cur["version"] and still["config"]["max_connections"] == 250

    # 预演非法配置：HTTP 200 + accepted=False，reason 可区分，生效配置不变
    bad = client.post("/config/preview", json={"config": {"max_connections": 999999}}).json()
    assert bad["accepted"] is False and bad["reason"] == "out_of_range"
    stale = client.post("/config/preview", json={
        "expected_version": 9999, "config": {"max_connections": 10},
    }).json()
    assert stale["accepted"] is False and stale["reason"] == "stale_version"

    # 审计：成功的更新可查到版本、时间与改动内容；预演不在其中
    audit = client.get("/config/audit").json()
    kinds = [(r["kind"], r["version"]) for r in audit["records"]]
    logger.info("审计列表=%s", kinds)
    assert any(r["kind"] == "update" and r["version"] == cur["version"]
               and r["changes"]["keys"]["changed"]["max_connections"]["new"] == 250
               for r in audit["records"])
    assert all("timestamp" in r and r["timestamp"] for r in audit["records"])


def test_rollback_evicted_returns_distinct_410():
    # 回滚一个从未存在的版本 -> 404 version_not_found
    nf = client.post("/config/rollback", json={"target_version": 999_999})
    logger.info("回滚不存在版本响应=%s", nf.json())
    assert nf.status_code == 404 and nf.json()["detail"]["reason"] == "version_not_found"
