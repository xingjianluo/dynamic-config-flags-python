"""分层配置：来源优先级确定且可解释，合并结果可复现。

优先级（高 -> 低）：RUNTIME > ENV > FILE > DEFAULT。
同键冲突时高优先级来源整体覆盖低优先级来源；每个键的生效来源都会被记录，
使合并结果可解释、可复现。
"""
from __future__ import annotations

import enum
from typing import Any


class Source(enum.IntEnum):
    """配置来源，数值越大优先级越高。"""

    DEFAULT = 0
    FILE = 1
    ENV = 2
    RUNTIME = 3


def merge_layers(
    layers: list[tuple[Source, dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """合并多层配置。

    参数为 (来源, 该层配置) 列表，顺序任意，内部按来源优先级重排。
    返回 (合并后的配置, 每个键最终来自哪个来源)。
    注意：flags 以整表覆盖，不做层内深合并，保证来源解释唯一、无部分生效状态。
    """
    ordered = sorted(layers, key=lambda item: int(item[0]))
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for source, layer in ordered:
        for key, value in layer.items():
            merged[key] = value
            provenance[key] = source.name
    return merged, provenance
