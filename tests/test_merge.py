"""配置优先级与非法配置拒绝原因的测试。"""

import logging

import pytest

from app.errors import ConfigRejectedError, RejectionReason
from app.merge import merge_layers, validate_layer
from app.sources import SourceLayer, load_defaults

logger = logging.getLogger(__name__)


def test_priority_high_layer_overrides_low():
    layers = [
        (SourceLayer.DEFAULTS, load_defaults()),
        (SourceLayer.FILE, {"service.port": 9000, "service.log_level": "DEBUG"}),
        (SourceLayer.ENV, {"service.port": 7000}),
    ]
    merged = merge_layers(layers)
    logger.info("输入分层: %s", layers)
    logger.info("合并结果: %s", merged)
    # ENV(20) > FILE(10) > DEFAULTS(0)
    assert merged["service.port"] == 7000
    assert merged["service.log_level"] == "DEBUG"
    assert merged["service.name"] == "dynamic-config-flags"


def test_layer_order_independent_of_input_order():
    a = merge_layers(
        [(SourceLayer.ENV, {"service.port": 1}), (SourceLayer.FILE, {"service.port": 2})]
    )
    b = merge_layers(
        [(SourceLayer.FILE, {"service.port": 2}), (SourceLayer.ENV, {"service.port": 1})]
    )
    assert a == b, "合并结果必须只取决于层级，与传入顺序无关"
    assert a["service.port"] == 1


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({"no.such.key": 1}, RejectionReason.UNKNOWN_KEY),
        ({"service.port": "8080"}, RejectionReason.TYPE_MISMATCH),
        ({"service.port": True}, RejectionReason.TYPE_MISMATCH),
        ({"features.enable_metrics": 1}, RejectionReason.TYPE_MISMATCH),
        ({"service.port": 0}, RejectionReason.OUT_OF_RANGE),
        ({"service.port": 70000}, RejectionReason.OUT_OF_RANGE),
        ({"service.log_level": "VERBOSE"}, RejectionReason.OUT_OF_RANGE),
    ],
)
def test_invalid_config_rejected_with_distinguishable_reason(config, reason):
    logger.info("输入配置: %s, 期望拒绝原因: %s", config, reason.value)
    with pytest.raises(ConfigRejectedError) as exc_info:
        validate_layer(config)
    logger.info("实际拒绝: %s", exc_info.value)
    assert exc_info.value.reason is reason
    assert exc_info.value.keys, "拒绝必须指明问题键"
