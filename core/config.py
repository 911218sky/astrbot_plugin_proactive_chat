# core/config.py — 配置管理
"""驗證、會話配置查詢、備份。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiofiles
import aiofiles.os as aio_os

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

_LOG_TAG = "[主動訊息]"


# ── 驗證 ──────────────────────────────────────────────────


async def validate_config(config: AstrBotConfig) -> None:
    """驗證插件配置的完整性和有效性。"""
    try:
        for label, settings_key, sessions_key in (
            ("私聊", "private_settings", "private_sessions"),
            ("群聊", "group_settings", "group_sessions"),
        ):
            settings = config.get(settings_key, {})
            if not settings.get("enable", False):
                continue
            sessions = config.get(sessions_key, [])
            has_personal = any(
                sc.get("enable") and sc.get("session_id") for sc in sessions
            )
            has_list = bool(settings.get("session_list"))
            if not has_personal and not has_list:
                logger.warning(
                    f"{_LOG_TAG} {label}主動訊息已啟用但未配置任何會話"
                    f"（既無個性化配置也無 session_list）。"
                )
            sched = settings.get("schedule_settings", {})
            if sched.get("min_interval_minutes", 0) > sched.get(
                "max_interval_minutes", 999
            ):
                logger.warning(
                    f"{_LOG_TAG} {label}配置中最小間隔大於最大間隔，將自動調整。"
                )

        logger.info(f"{_LOG_TAG} 配置驗證完成。")
    except Exception as e:
        logger.error(f"{_LOG_TAG} 配置驗證出錯: {e}")
        raise


# ── 會話配置查詢 ──────────────────────────────────────────


def get_session_config(config: AstrBotConfig, session_id: str) -> dict | None:
    """根據會話 ID 取得對應配置（個性化優先，全域兜底）。"""
    from .utils import is_private_session, parse_session_id

    parsed = parse_session_id(session_id)
    if not parsed:
        return None

    _, msg_type, target_id = parsed

    if is_private_session(msg_type):
        return _match_session(
            config,
            target_id,
            sessions_key="private_sessions",
            settings_key="private_settings",
            session_type="private",
        )
    if "Group" in msg_type:
        return _match_session(
            config,
            target_id,
            sessions_key="group_sessions",
            settings_key="group_settings",
            session_type="group",
        )
    return None


def _match_session(
    config: AstrBotConfig,
    target_id: str,
    *,
    sessions_key: str,
    settings_key: str,
    session_type: str,
) -> dict | None:
    """通用的會話配置匹配：先查個性化列表，再查全域 session_list。"""
    # 1) 個性化配置
    for sc in config.get(sessions_key, ()):
        cid = str(sc.get("session_id", ""))
        if cid and (target_id == cid or target_id.endswith(f":{cid}")):
            if not sc.get("enable", False):
                return None
            out = sc.copy()
            out["_session_name"] = sc.get("session_name", "")
            out["_session_type"] = session_type
            return out

    # 2) 全域 session_list
    settings = config.get(settings_key, {})
    if not settings.get("enable", False):
        return None
    if target_id in settings.get("session_list", ()):
        out = settings.copy()
        out["_session_type"] = session_type
        out["_from_session_list"] = True
        return out
    return None


# ── 備份 ──────────────────────────────────────────────────


async def backup_configurations(config: AstrBotConfig, data_dir: Path) -> None:
    """備份使用者配置快照及 Prompt 彙總。"""
    try:
        await aio_os.makedirs(data_dir, exist_ok=True)

        # 配置快照
        snap_file = data_dir / "user_config_snapshot.json"
        async with aiofiles.open(snap_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(dict(config), indent=2, ensure_ascii=False))

        # Prompt 彙總
        lines: list[str] = [
            "# 🧠 主動訊息 Prompt 彙總備份\n",
            f"> 備份時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        ]

        def _add(title: str, settings: dict) -> None:
            prompt = settings.get("proactive_prompt", "")
            if prompt:
                lines.extend((f"## {title}\n", "```text", prompt, "```\n"))

        _add("私聊全域 Prompt", config.get("private_settings", {}))
        _add("群聊全域 Prompt", config.get("group_settings", {}))

        for label, key in (("私聊", "private_sessions"), ("群聊", "group_sessions")):
            for i, s in enumerate(config.get(key, ()), 1):
                if s.get("session_id") and s.get("enable"):
                    name = s.get("session_name", "未命名")
                    _add(f"{label}會話 #{i} ({s['session_id']} - {name})", s)

        prompt_file = data_dir / "prompts_collection.md"
        async with aiofiles.open(prompt_file, "w", encoding="utf-8") as f:
            await f.write("\n".join(lines))

        logger.info(f"{_LOG_TAG} 配置快照與 Prompt 彙總已備份至: {data_dir}")
    except Exception as e:
        logger.warning(f"{_LOG_TAG} 配置備份流程出錯: {e}")
