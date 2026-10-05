"""配置变更摘要：比较两份配置，给出键级与开关级的结构化差异。"""
from __future__ import annotations

from typing import Any


def diff_configs(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """返回结构化差异：哪些普通键新增/移除/修改，哪些开关新增/移除/修改。

    - 普通配置键（除 flags 外）逐键比较，changed 给出 (old, new)；
    - flags 以整表写入，这里仍逐开关、逐字段比较，便于审计与预演说明影响面；
    - 仅做只读比较，不修改入参。返回结构中引用的值来自入参，
      调用方需要对外暴露时应自行深拷贝。
    """
    old_keys = {k: v for k, v in old.items() if k != "flags"}
    new_keys = {k: v for k, v in new.items() if k != "flags"}
    old_flags = old.get("flags", {}) or {}
    new_flags = new.get("flags", {}) or {}

    keys_added = {k: new_keys[k] for k in new_keys.keys() - old_keys.keys()}
    keys_removed = sorted(old_keys.keys() - new_keys.keys())
    keys_changed: dict[str, dict[str, Any]] = {}
    for k in sorted(old_keys.keys() & new_keys.keys()):
        if old_keys[k] != new_keys[k]:
            keys_changed[k] = {"old": old_keys[k], "new": new_keys[k]}

    flags_added = {name: new_flags[name] for name in new_flags.keys() - old_flags.keys()}
    flags_removed = sorted(old_flags.keys() - new_flags.keys())
    flags_changed: dict[str, dict[str, dict[str, Any]]] = {}
    for name in sorted(old_flags.keys() & new_flags.keys()):
        old_rule = old_flags[name]
        new_rule = new_flags[name]
        fields: dict[str, dict[str, Any]] = {}
        for field_name in sorted(set(old_rule) | set(new_rule)):
            in_old = field_name in old_rule
            in_new = field_name in new_rule
            if not in_old or not in_new or old_rule[field_name] != new_rule[field_name]:
                fields[field_name] = {
                    "old": old_rule.get(field_name) if in_old else None,
                    "new": new_rule.get(field_name) if in_new else None,
                    "present_old": in_old,
                    "present_new": in_new,
                }
        if fields:
            flags_changed[name] = fields

    return {
        "keys": {"added": keys_added, "removed": keys_removed, "changed": keys_changed},
        "flags": {"added": flags_added, "removed": flags_removed, "changed": flags_changed},
    }


def is_empty_diff(diff: dict[str, Any]) -> bool:
    """差异是否为空（两份配置完全一致）。"""
    k, f = diff["keys"], diff["flags"]
    return not (k["added"] or k["removed"] or k["changed"]
                or f["added"] or f["removed"] or f["changed"])
