"""只读预演：接受/拒绝结论可区分、变化摘要正确、不留任何副作用、并发不串扰。"""
import logging
import threading

from app.schema import ConfigRejected
from app.store import ConfigStore

logger = logging.getLogger("test")


def test_preview_accepted_reports_changes_and_future_provenance():
    store = ConfigStore(
        {"max_connections": 100, "debug": False},
        {"max_connections": "FILE", "debug": "DEFAULT"},
    )
    candidate = {
        "max_connections": 200,
        "debug": True,
        "flags": {"new-ui": {"enabled": True, "percentage": 30, "allowlist": ["vip-1"]}},
    }
    logger.info("预演输入=%s base_version=%s", candidate, store.version)
    r = store.preview(candidate, expected_version=0)
    logger.info("预演结论 accepted=%s would_be=%s 变化=%s 来源预测=%s",
                r["accepted"], r["would_be_version"], r["changes"], r["provenance_after"])
    assert r["accepted"] is True
    assert r["reason"] is None
    assert r["base_version"] == 0 and r["would_be_version"] == 1
    assert r["changes"]["keys"]["changed"]["max_connections"] == {"old": 100, "new": 200}
    assert r["changes"]["keys"]["changed"]["debug"] == {"old": False, "new": True}
    assert "new-ui" in r["changes"]["flags"]["added"]
    # 预演预测的来源：整份写入后所有键来源变为 RUNTIME
    assert r["provenance_after"]["max_connections"] == "RUNTIME"
    assert r["provenance_after"]["flags.new-ui"] == "RUNTIME"


def test_preview_rejected_reasons_are_distinguishable():
    store = ConfigStore({"max_connections": 100})
    store.update({"max_connections": 200}, 0)
    cases = [
        ({"nope": 1}, None, "unknown_key"),
        ({"max_connections": 999999}, None, "out_of_range"),
        ({"max_connections": "x"}, None, "type_mismatch"),
        ({"max_connections": 300}, 0, "stale_version"),
    ]
    for raw, ver, reason in cases:
        logger.info("预演输入=%s expected=%s 期望原因=%s", raw, ver, reason)
        r = store.preview(raw, ver)
        assert r["accepted"] is False and r["reason"] == reason
        assert r["changes"] is None and r["provenance_after"] is None
        # 拒绝时仍告知当前基准版本，便于调用方重试
        assert r["base_version"] == store.version


def test_preview_is_readonly_no_version_no_audit_no_effect():
    store = ConfigStore({"max_connections": 100}, clock=lambda: "T")
    before = store.get_effective()
    candidate = {"max_connections": 777}
    for _ in range(5):
        r = store.preview(candidate, expected_version=0)
        assert r["accepted"] is True and r["would_be_version"] == 1  # 版本号永远不被占用
    # 预演非法候选同样不留痕
    store.preview({"nope": 1})
    after = store.get_effective()
    logger.info("多次预演后: version=%s config=%s", after.version, after.config)
    assert after.version == before.version
    assert after.config == before.config
    assert store.audit_records() == []
    assert store.history_versions() == [0]
    # 预演之后真实写入仍然拿到版本 1，证明预演没有插队
    assert store.update(candidate, 0) == 1


def test_preview_return_values_independent_from_store_and_each_other():
    store = ConfigStore({"max_connections": 100})
    r1 = store.preview({"max_connections": 200}, 0)
    r1["changes"]["keys"]["changed"]["max_connections"]["new"] = 9999
    r1["provenance_after"]["max_connections"] = "FILE"
    r2 = store.preview({"max_connections": 200}, 0)
    # 调用方篡改预演返回值，不影响后续预演与生效配置读取
    assert r2["changes"]["keys"]["changed"]["max_connections"]["new"] == 200
    assert r2["provenance_after"]["max_connections"] == "RUNTIME"
    assert store.get_config()["max_connections"] == 100

    view = store.get_effective()
    view.config["max_connections"] = 4242
    view.provenance["max_connections"] = "ENV"
    assert store.get_config()["max_connections"] == 100
    assert store.get_provenance()["max_connections"] == "DEFAULT"
    recs = store.audit_records()  # 无审计时也应是独立对象
    recs.append("x")
    assert store.audit_records() == []


def test_concurrent_previews_never_observe_partial_or_cross_state():
    store = ConfigStore({
        "max_connections": 100, "request_timeout_ms": 3000,
        "flags": {"f": {"percentage": 0}},
    })
    anomalies: list[str] = []
    payload_a = {"max_connections": 200, "request_timeout_ms": 5000,
                 "flags": {"f": {"percentage": 0}}}
    payload_b = {"max_connections": 300, "request_timeout_ms": 9000,
                 "flags": {"f": {"percentage": 100}}}

    def previewer() -> None:
        # 多调用方并发预演两份候选；每个预演结论必须自洽，不能串扰成半新半旧
        idx = 0
        while not stop.is_set():
            cand = payload_a if idx % 2 == 0 else payload_b
            idx += 1
            r = store.preview(cand)
            if not r["accepted"]:
                anomalies.append(f"合法候选被拒: {r['reason']}")
                continue
            chg = r["changes"]
            after = r["provenance_after"]
            conn_chg = chg["keys"]["changed"].get("max_connections")
            pct_chg = chg["flags"]["changed"].get("f", {}).get("percentage")
            new_conn = cand["max_connections"]  # 预演后的生效值必然等于候选本身
            new_pct = cand["flags"]["f"]["percentage"]
            # 若该项在差异里出现，其 new 必须与候选完全一致（否则就是串扰）
            if conn_chg is not None and conn_chg["new"] != new_conn:
                anomalies.append(f"键变化串扰: {conn_chg}")
            if pct_chg is not None and pct_chg["new"] != new_pct:
                anomalies.append(f"开关变化串扰: {pct_chg}")
            # 来源预测必须与候选完整对应：候选里两个键都来自 RUNTIME
            if after.get("max_connections") != "RUNTIME" or after.get("flags.f") != "RUNTIME":
                anomalies.append("预演来源预测错误")
            # 预演报告的 would_be_version 必须基于它实际看到的同一快照
            if r["base_version"] + 1 != r["would_be_version"]:
                anomalies.append("预演基准版本与预测版本不配套")

    stop = threading.Event()
    threads = [threading.Thread(target=previewer) for _ in range(6)]
    for t in threads:
        t.start()

    # 单写线程串行推进版本，交替写两份完整配置，期间并发预演不得读到混搭状态
    def writer() -> None:
        v = 0
        for i in range(300):
            payload = payload_a if i % 2 == 0 else payload_b
            try:
                v = store.update(payload, v)
            except ConfigRejected:
                anomalies.append("合法真实更新被拒")

    wt = threading.Thread(target=writer)
    wt.start()
    wt.join(timeout=10)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    logger.info("并发预演+更新结束，最终版本=%s 异常=%s", store.version, anomalies)
    assert anomalies == []
    # 预演不改变版本推进之外的任何东西；生效配置始终是某一份完整合法配置
    view = store.get_effective()
    assert view.config["max_connections"] in (200, 300)
    assert view.config["request_timeout_ms"] in (5000, 9000)
    pct = view.config["flags"]["f"]["percentage"]
    assert pct in (0, 100)
    # 配对不变式：200 必与 5000ms/0% 同版，300 必与 9000ms/100% 同版，无半新半旧
    if view.config["max_connections"] == 200:
        assert view.config["request_timeout_ms"] == 5000 and pct == 0
    else:
        assert view.config["request_timeout_ms"] == 9000 and pct == 100
    assert view.provenance["flags.f"] == "RUNTIME"
