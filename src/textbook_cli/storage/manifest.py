"""只记录已完成文件的 SQLite 清单；调用者负责输出目录互斥。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..core.errors import AppError


class Manifest:
    FIELDS = {
        "resource_id", "artifact_id", "upstream_revision", "expected_md5", "expected_size",
        "relative_path", "completed_at", "source_md5", "source_sha256", "source_size",
        "final_sha256", "final_size", "bookmarks_requested", "bookmarks_applied", "warnings",
    }

    def __init__(self, output_root: Path):
        self.path = Path(output_root) / ".textbook" / "manifest.sqlite3"
        self.connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=0)
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise sqlite3.DatabaseError("清单模式不兼容")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS completed (resource_id TEXT NOT NULL, "
                "artifact_id TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(resource_id, artifact_id))"
            )
            self.connection.execute("PRAGMA user_version=1")
            self.connection.commit()
        except (OSError, sqlite3.Error):
            self.close()
            raise AppError("FILESYSTEM_ERROR", "无法打开下载清单，请检查权限或数据库是否损坏") from None

    def get(self, resource_id: str, artifact_id: str) -> dict | None:
        try:
            row = self.connection.execute(
                "SELECT record FROM completed WHERE resource_id=? AND artifact_id=?",
                (resource_id, artifact_id),
            ).fetchone()
            return json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError, TypeError):
            raise AppError("FILESYSTEM_ERROR", "下载清单记录损坏，未将已有文件视作完成") from None

    def records(self) -> list[dict]:
        """返回全部完成记录；单行损坏时跳过，按身份的 get 仍会严格校验。"""
        try:
            rows = self.connection.execute("SELECT record FROM completed").fetchall()
        except sqlite3.Error:
            raise AppError("FILESYSTEM_ERROR", "无法读取下载清单，请检查权限或数据库是否损坏") from None
        result = []
        for row in rows:
            try:
                record = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            if isinstance(record, dict):
                result.append(record)
        return result

    def put(self, record: dict) -> None:
        safe = {k: v for k, v in record.items() if k in self.FIELDS}
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT OR REPLACE INTO completed(resource_id,artifact_id,record) VALUES(?,?,?)",
                    (safe["resource_id"], safe["artifact_id"], json.dumps(safe, ensure_ascii=False)),
                )
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            raise AppError("FILESYSTEM_ERROR", "文件已保存，但下载清单登记失败；请检查磁盘和权限") from None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
