"""启动期分层加载：默认值 < 配置文件 < 环境变量。

- DEFAULT：CONFIG_SCHEMA 中各字段 default
- FILE：通过环境变量 DYNAMIC_CONFIG_FILE 指定的 JSON 文件
- ENV：前缀 DYNCFG_ 的环境变量，值按 JSON 解析（如 DYNCFG_MAX_CONNECTIONS=200）

加载时先合并再做整份校验，任一层引入非法键/类型/越界都会整体报错，
避免带着非法配置启动。返回 (合并配置, 键级来源说明)。
"""
from __future__ import annotations

import json
import os
from typing import Any

from app.layers import Source, merge_layers
from app.schema import CONFIG_SCHEMA, validate_config

ENV_PREFIX = "DYNCFG_"


def defaults() -> dict[str, Any]:
    return {name: spec.default for name, spec in CONFIG_SCHEMA.items()}


def load_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        from app.schema import ConfigRejected, RejectReason

        raise ConfigRejected(RejectReason.TYPE_MISMATCH, "配置文件根节点必须是 JSON 对象")
    return data


def load_env(environ: dict[str, str] | None = None) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    result: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        field_name = key[len(ENV_PREFIX) :].lower()
        try:
            result[field_name] = json.loads(raw)
        except json.JSONDecodeError:
            from app.schema import ConfigRejected, RejectReason

            raise ConfigRejected(
                RejectReason.TYPE_MISMATCH, f"环境变量 {key} 的值不是合法 JSON: {raw!r}"
            )
    return result


def load_bootstrap(
    file_path: str | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """分层加载并整体校验，返回 (合并后的配置, 来源说明)。"""
    merged, provenance = merge_layers(
        [
            (Source.DEFAULT, defaults()),
            (Source.FILE, load_file(file_path)),
            (Source.ENV, load_env(environ)),
        ]
    )
    validated = validate_config(merged)
    return validated, provenance
