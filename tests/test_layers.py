"""分层配置优先级测试。"""
import logging

from app.layers import Source, merge_layers

logger = logging.getLogger("test")


def test_layer_priority_and_provenance():
    layers = [
        (Source.RUNTIME, {"max_connections": 300, "debug": True}),
        (Source.DEFAULT, {"service_name": "d", "max_connections": 100, "request_timeout_ms": 3000}),
        (Source.ENV, {"max_connections": 200}),
        (Source.FILE, {"service_name": "f", "max_connections": 150}),
    ]
    logger.info("输入分层: %s", [(s.name, c) for s, c in layers])
    merged, provenance = merge_layers(layers)
    logger.info("合并结果=%s 来源=%s", merged, provenance)

    assert merged == {
        "service_name": "f",        # FILE 高于 DEFAULT
        "max_connections": 300,     # RUNTIME 最高
        "request_timeout_ms": 3000,
        "debug": True,
    }
    assert provenance["max_connections"] == "RUNTIME"
    assert provenance["service_name"] == "FILE"
    assert provenance["request_timeout_ms"] == "DEFAULT"
    assert provenance["debug"] == "RUNTIME"


def test_merge_order_independent_and_reproducible():
    a = [(Source.FILE, {"a": 1}), (Source.ENV, {"a": 2})]
    b = [(Source.ENV, {"a": 2}), (Source.FILE, {"a": 1})]
    assert merge_layers(a) == merge_layers(b)
