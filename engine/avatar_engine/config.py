"""Session configuration: defaults + validation against contracts/config.schema.json.

A copy of the schema ships inside the package (``config.schema.json``); a unit
test keeps it byte-identical to ``contracts/config.schema.json``.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any


class ConfigError(ValueError):
    pass


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    text = resources.files("avatar_engine").joinpath("config.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _check_type(value: Any, types: str | list[str]) -> bool:
    for t in [types] if isinstance(types, str) else types:
        py = _TYPES[t]
        if t in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def _validate(value: Any, node: dict[str, Any], path: str, errors: list[str]) -> None:
    if "const" in node and value != node["const"]:
        errors.append(f"{path}: must be {node['const']!r}")
        return
    if "enum" in node and value not in node["enum"]:
        errors.append(f"{path}: {value!r} not in {node['enum']}")
        return
    if "type" in node and not _check_type(value, node["type"]):
        errors.append(f"{path}: expected {node['type']}, got {type(value).__name__}")
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            errors.append(f"{path}: {value} < minimum {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            errors.append(f"{path}: {value} > maximum {node['maximum']}")
    if isinstance(value, str) and "pattern" in node and not re.search(node["pattern"], value):
        errors.append(f"{path}: {value!r} does not match {node['pattern']}")
    if isinstance(value, list) and "items" in node:
        for i, item in enumerate(value):
            _validate(item, node["items"], f"{path}[{i}]", errors)
    if isinstance(value, dict) and "properties" in node:
        props = node["properties"]
        for req in node.get("required", []):
            if req not in value:
                errors.append(f"{path}.{req}: required")
        for key, sub in value.items():
            if key not in props:
                if node.get("additionalProperties", True) is False:
                    errors.append(f"{path}.{key}: unknown property")
                continue
            _validate(sub, props[key], f"{path}.{key}", errors)


def _defaults(node: dict[str, Any]) -> Any:
    if node.get("type") == "object" or "properties" in node:
        out = {}
        for key, sub in node.get("properties", {}).items():
            d = _defaults(sub)
            if d is not None:
                out[key] = d
        return out
    if "default" in node:
        return copy.deepcopy(node["default"])
    return None


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_config() -> dict[str, Any]:
    cfg = _defaults(schema())
    cfg.update({"schema_version": 2, "engine": "lite_adaptive", "profile_id": "default", "drive_mode": "webcam"})
    return cfg


ENGINE_DRIVE_MODES = {
    "lite_cpu_audio": {"audio"},
    "lite_cpu_swap": {"webcam"},
    "lite_adaptive": {"webcam"},
    "accelerated_swap": {"webcam"},
    "synthetic_head": {"audio", "synthetic"},
}


def validate(cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    _validate(cfg, schema(), "config", errors)
    engine = cfg.get("engine")
    mode = cfg.get("drive_mode")
    if engine in ENGINE_DRIVE_MODES and mode not in ENGINE_DRIVE_MODES[engine]:
        errors.append(f"config.drive_mode: {mode!r} is not valid for engine {engine!r}")
    return errors


def load(user_cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge user config over defaults, validate, and apply profile implications."""
    if not isinstance(user_cfg, dict):
        raise ConfigError("config must be a JSON object")
    raw_errors = validate_partial(user_cfg)
    if raw_errors:
        raise ConfigError("; ".join(raw_errors))
    cfg = _merge(default_config(), user_cfg)
    if cfg.get("user_profile") == "cpu_only":
        cfg["inference"]["cpu_only"] = True
    if cfg["inference"]["cpu_only"]:
        cfg["inference"]["provider"] = "cpu"
        cfg["inference"]["provider_priority"] = ["cpu"]
    errors = validate(cfg)
    if errors:
        raise ConfigError("; ".join(errors))
    return cfg


def validate_partial(user_cfg: dict[str, Any]) -> list[str]:
    """Validate only what the user supplied (unknown keys, types, ranges)."""
    errors: list[str] = []
    node = dict(schema())
    node.pop("required", None)
    _validate(user_cfg, node, "config", errors)
    return errors
