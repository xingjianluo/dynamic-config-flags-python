"""分层配置来源：优先级确定且可解释。

优先级（低 -> 高）：DEFAULTS < FILE < ENV < RUNTIME。
RUNTIME 层由 ConfigStore.update 在运行时写入，不在这里加载。
"""

from __future__ import annotations

import enum
import json
import os
from typing import Any

from app.schema import CONFIG_SCHEMA


class SourceLayer(enum.IntEnum):
    """配置来源层级，数值越大优先级越高。"""

    DEFAULTS = 0
    FILE = 10
    ENV = 20
    RUNTIME = 30


def load_defaults() -> dict[str, Any]:
    """内置默认配置。"""
    return {
        "service.name": "dynamic-config-flags",
        "service.port": 8080,
        "service.log_level": "INFO",
        "limits.max_requests": 1000,
        "limits.timeout_ms": 3000,
        "features.enable_metrics": False,
    }


def load_file(path: str | None) -> dict[str, Any]:
    """从 JSON 文件加载配置；path 为 None 时返回空。文件必须是扁平的键值对象。"""
    if path is None:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"config file must contain a JSON object: {path}")
    return dict(data)


def _parse_env_value(key: str, raw: str) -> Any:
    """按模式类型解析环境变量字符串；无法解析时保留原值，交给校验层拒绝。"""
    spec = CONFIG_SCHEMA.get(key)
    if spec is None:
        return raw
    if spec.type_ is bool:
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        return raw
    if spec.type_ is int:
        try:
            return int(raw.strip())
        except ValueError:
            return raw
    return raw


def load_env(environ: dict[str, str] | None = None, prefix: str = "APP_") -> dict[str, Any]:
    """从环境变量加载配置：APP_SERVICE_PORT=9000 -> service.port。"""
    env = os.environ if environ is None else environ
    config: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.startswith(prefix):
            continue
        key = name[len(prefix):].lower().replace("_", ".")
        # 已知键按点号还原：service.port / limits.max.requests -> limits.max_requests
        key = _resolve_key(key)
        config[key] = _parse_env_value(key, raw)
    return config


def _resolve_key(dotted: str) -> str:
    """把环境变量派生的点分键还原为模式中的已知键；无法还原时原样返回（将被判为未知键）。"""
    if dotted in CONFIG_SCHEMA:
        return dotted
    # 尝试把下划线还原，匹配已知键（如 features.enable.metrics -> features.enable_metrics）
    for known in CONFIG_SCHEMA:
        if known.replace("_", ".") == dotted:
            return known
    return dotted
