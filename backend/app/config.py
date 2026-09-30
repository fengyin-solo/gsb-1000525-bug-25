"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    env_dir = os.environ.get("ASSAY_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    # 默认放在后端目录下，克隆即用；docker 里可挂卷覆盖
    return Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class Settings:
    app_name: str = "地质勘探数据管理平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    data_dir: Path = field(default_factory=_default_data_dir)


settings = Settings()
