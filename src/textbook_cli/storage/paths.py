"""平台标准目录；计算路径不创建目录。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    cache_dir: Path

    @classmethod
    def default(cls) -> AppPaths:
        from platformdirs import user_cache_path, user_config_path
        return cls(user_config_path("textbook-cli", appauthor=False),
                   user_cache_path("textbook-cli", appauthor=False))

    @property
    def config_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def cache_file(self) -> Path:
        return self.cache_dir / "catalog.json"
