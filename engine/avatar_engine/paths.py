"""Per-user local data locations (never shared, never uploaded)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("AVATAR_LIVE_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "AvatarLive"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "avatar-live"
    base.mkdir(parents=True, exist_ok=True)
    return base


def sub_dir(name: str) -> Path:
    p = data_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def profiles_dir() -> Path:
    return sub_dir("profiles")


def models_dir() -> Path:
    override = os.environ.get("AVATAR_LIVE_MODELS_DIR")
    if override:
        return Path(override)
    return sub_dir("models")


def benchmarks_dir() -> Path:
    return sub_dir("benchmarks")


def logs_dir() -> Path:
    return sub_dir("logs")
