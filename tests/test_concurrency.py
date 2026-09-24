"""并发读写：热更新期间读取方不得观察到部分生效的中间状态。"""

import logging
import threading

from app.store import ConfigStore

logger = logging.getLogger(__name__)

ROUNDS = 200
READERS = 8


def make_store() -> ConfigStore:
    return ConfigStore(
        {"service.port": 8000},
        {"dark_mode": {"enabled": True, "rollout": 0, "allowlist": [], "denylist": []}},
    )


def test_no_partial_state_during_hot_updates():
    """writer 每次原子地同时改 port 与 rollout（两者数值绑定），
    reader 读到的任何快照都必须满足该绑定关系，否则说明看到了中间状态。"""
    store = make_store()
    violations: list[str] = []
    stop = threading.Event()

    def writer() -> None:
        for i in range(1, ROUNDS + 1):
            while True:
                version = store.snapshot().version
                try:
                    store.update(
                        {"service.port": 8000 + i},
                        {"dark_mode": {"enabled": True, "rollout": i % 101,
                                       "allowlist": [], "denylist": []}},
                        expected_version=version,
                    )
                    break
                except Exception:
                    continue  # 多 writer 场景下的版本冲突重试
        stop.set()

    def reader() -> None:
        while not stop.is_set():
            snap = store.snapshot()
            port, rollout = snap.config["service.port"], snap.flags["dark_mode"]["rollout"]
            if port - 8000 > 0 and (port - 8000) % 101 != rollout:
                violations.append(f"v{snap.version}: port={port} rollout={rollout}")

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(READERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.snapshot()
    logger.info("最终快照: version=%d port=%d rollout=%d",
                final.version, final.config["service.port"], final.flags["dark_mode"]["rollout"])
    logger.info("中间状态违规数: %d", len(violations))
    assert not violations, f"观察到部分生效的中间状态: {violations[:5]}"
    assert final.config["service.port"] == 8000 + ROUNDS
    assert final.flags["dark_mode"]["rollout"] == ROUNDS % 101


def test_concurrent_writers_single_winner_per_version():
    """并发写同一版本时只有一个成功，其余收到过期版本拒绝。"""
    from app.errors import StaleVersionError

    store = make_store()
    results: list[str] = []
    lock = threading.Lock()

    def writer(idx: int) -> None:
        try:
            store.update({"service.port": 9000 + idx}, expected_version=1)
            with lock:
                results.append("ok")
        except StaleVersionError:
            with lock:
                results.append("stale")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logger.info("并发写结果: ok=%d stale=%d", results.count("ok"), results.count("stale"))
    assert results.count("ok") == 1
    assert results.count("stale") == 9
    assert store.snapshot().version == 2
