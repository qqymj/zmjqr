"""配置加载：读取 config/config.toml（标准库 tomllib，无需外部依赖）。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

# 项目根目录（robot/core/config.py -> 上溯三级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载 TOML 配置；环境变量覆盖留待 M2 接入硬件时补。"""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with config_path.open("rb") as f:
        return tomllib.load(f)
