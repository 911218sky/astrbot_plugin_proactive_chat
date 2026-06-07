"""SQLite storage for the plugin's latest runtime state.

Only the newest ``session_data`` snapshot is kept.  This mirrors the old JSON
shape used by the plugin while making writes atomic and restart-safe.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiosqlite
from astrbot.api import logger


_STATE_KEY_SESSION_DATA = "session_data"
_LOG_TAG = "[主動訊息]"


class StateStoreCorruptionError(RuntimeError):
    """Raised when the latest state snapshot cannot be safely decoded."""


class ProactiveStateStore:
    """Small SQLite store that keeps the latest plugin state snapshot."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(str(self.db_path))
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode = WAL")
        await self.connection.execute("PRAGMA busy_timeout = 10000")
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def load_session_data(self) -> dict[str, dict]:
        if self.connection is None:
            return {}
        cursor = await self.connection.execute(
            "SELECT value FROM plugin_state WHERE key = ?",
            (_STATE_KEY_SESSION_DATA,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return {}

        payload = row["value"]
        if not payload.strip():
            return {}
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            logger.error(
                f"{_LOG_TAG} 插件狀態資料庫 JSON 無法解析，已停止載入以避免覆蓋原資料。"
            )
            raise StateStoreCorruptionError("plugin_state.session_data JSON 已損壞") from exc
        if not isinstance(data, dict):
            logger.error(
                f"{_LOG_TAG} 插件狀態資料庫格式不正確，已停止載入以避免覆蓋原資料。"
            )
            raise StateStoreCorruptionError("plugin_state.session_data 不是 dict")
        return data

    async def save_session_data(self, session_data: dict[str, dict]) -> None:
        if self.connection is None:
            return
        payload = json.dumps(session_data, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            await self.connection.execute(
                """
                INSERT INTO plugin_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (_STATE_KEY_SESSION_DATA, payload, time.time()),
            )
            await self.connection.commit()
