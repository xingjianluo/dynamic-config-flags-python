"""HTTP 端到端：来源追溯、审计查询、只读预演。"""
import logging

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

logger = logging.getLogger("test")
client = TestClient(main.app)


def test_provenance_audit_and_dry_run_over_http():
    base = client.get("/config").json()
    audit_before = len(client.get("/config/audit").json()["records"])
    logger.info("初始: version=%s provenance=%s 已有审计=%d 条",
                base["version"], base["provenance"], audit_before)
    assert base["provenance"]  # 启动分层来源可见

    # 只读预演：可接受，给出摘要，不产生副作用
    candidate = {"max_connections": 256,
                 "flags": {"beta": {"enabled": True, "percentage": 30}}}
    dry = client.post("/config/dry-run",
                      json={"expected_version": base["version"], "config": candidate})
    assert dry.status_code == 200
    body = dry.json()
    logger.info("预演响应=%s", body)
    assert body["accepted"] is True
    assert body["changes"]["max_connections"]["new"] == 256
    assert body["provenance_changes"]["max_connections"]["new"] == "RUNTIME"
    assert client.get("/config").json()["version"] == base["version"]  # 未生效
    # 未写审计（条数与预演前一致）
    assert len(client.get("/config/audit").json()["records"]) == audit_before

    # 预演拒绝：原因可区分
    rejected = client.post("/config/dry-run",
                           json={"expected_version": base["version"], "config": {"nope": 1}})
    assert rejected.json()["accepted"] is False
    assert rejected.json()["reason"] == "unknown_key"

    # 真实更新后：来源同步为 RUNTIME，审计出现一条记录
    upd = client.post("/config", json={"expected_version": base["version"], "config": candidate})
    assert upd.status_code == 200
    after = client.get("/config").json()
    logger.info("更新后来源=%s", after["provenance"])
    assert after["provenance"]["max_connections"] == "RUNTIME"
    assert after["provenance"]["flags.beta"] == "RUNTIME"
    audit = client.get("/config/audit").json()
    assert audit["capacity"] >= 1 and len(audit["records"]) == audit_before + 1
    record = audit["records"][-1]
    logger.info("审计记录=%s", record)
    assert record["kind"] == "update" and record["version"] == after["version"]
    assert record["changes"]["max_connections"]["new"] == 256

    # 回滚后来源与值一起还原，审计追加 rollback 记录
    rb = client.post("/config/rollback", json={"target_version": base["version"]})
    assert rb.status_code == 200
    restored = client.get("/config").json()
    assert restored["provenance"] == base["provenance"]
    records = client.get("/config/audit").json()["records"]
    assert records[-1]["kind"] == "rollback"
    assert records[-1]["target_version"] == base["version"]
