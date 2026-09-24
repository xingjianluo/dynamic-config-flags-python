"""并发读取与热更新：不得观察到部分生效的中间状态。"""
import logging
import threading

from app.schema import ConfigRejected, RejectReason
from app.store import ConfigStore

logger = logging.getLogger("test")


def test_readers_never_observe_partial_state_during_updates():
    store = ConfigStore({
        "max_connections": 100, "request_timeout_ms": 3000,
        "flags": {"f": {"enabled": True, "percentage": 0}},
    })
    stop = threading.Event()
    anomalies: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            snap = store.snapshot()
            cfg = snap.config
            # 不变式：每个版本中两字段必然同时存在且都合法 -> 看不到部分生效
            if not (1 <= cfg["max_connections"] <= 65535 and 1 <= cfg["request_timeout_ms"] <= 600000):
                anomalies.append(f"非法中间态: {cfg}")
            # 同一不可变快照内，判定与快照配置必须自洽且可复现
            from app.flags import evaluate
            rule = snap.rules.get("f")
            if rule is None:
                anomalies.append(f"版本 {snap.version} 缺少开关规则")
                continue
            a = evaluate(rule, "user-9")
            b = evaluate(rule, "user-9")
            if a != b:
                anomalies.append("同一快照内主体判定不一致")
            expected_pct = cfg["flags"]["f"]["percentage"]
            if expected_pct == 0 and a.allowed:
                anomalies.append(f"版本 {snap.version} percentage=0 却放行")
            if expected_pct == 100 and not a.allowed:
                anomalies.append(f"版本 {snap.version} percentage=100 却拒绝")

    readers = [threading.Thread(target=reader) for _ in range(8)]
    for t in readers:
        t.start()

    # 热更新交替写两份完整配置；偶发穿插非法配置，应被拒绝且无影响
    valid_a = {"max_connections": 200, "request_timeout_ms": 5000, "flags": {"f": {"percentage": 0}}}
    valid_b = {"max_connections": 300, "request_timeout_ms": 9000, "flags": {"f": {"percentage": 100}}}
    invalid = {"max_connections": 999999}
    version = 0
    for i in range(200):
        payload = valid_a if i % 2 == 0 else valid_b
        try:
            version = store.update(payload, version)
        except ConfigRejected:
            pass
        try:
            store.update(invalid, version)
        except ConfigRejected as exc:
            assert exc.reason is RejectReason.OUT_OF_RANGE

    stop.set()
    for t in readers:
        t.join()
    logger.info("热更新 200 轮完成，最终版本=%s", store.version)
    assert anomalies == []


def test_concurrent_competing_updates_only_one_wins():
    store = ConfigStore({"max_connections": 100})
    results: list[object] = []
    barrier = threading.Barrier(6)

    def writer(value: int) -> None:
        barrier.wait()
        try:
            results.append(("ok", value, store.update({"max_connections": value}, 0)))
        except ConfigRejected as exc:
            results.append(("reject", value, exc.reason))

    threads = [threading.Thread(target=writer, args=(100 + i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r[0] == "ok"]
    rejects = [r for r in results if r[0] == "reject"]
    logger.info("并发竞争更新: 成功=%s 拒绝=%s", oks, [(r[1], r[2].value) for r in rejects])
    assert len(oks) == 1
    assert all(r[2] is RejectReason.STALE_VERSION for r in rejects)
    assert store.version == 1
    assert store.get_config()["max_connections"] == oks[0][1]
