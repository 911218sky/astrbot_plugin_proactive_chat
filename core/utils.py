# core/utils.py — 通用工具函數
"""免打擾判斷、UMO 解析、日誌格式化、平台解析。"""

from __future__ import annotations

import json
import re
import zoneinfo
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from astrbot.core.platform.platform import Platform

from astrbot.api import logger
from astrbot.core.platform.platform import PlatformStatus

# ── 常數 ──────────────────────────────────────────────────

_FRIEND_KEYWORDS = frozenset(("Friend", "Private"))
_LOG_TAG = "[主動訊息]"

# 訊息類型常數
MSG_TYPE_FRIEND = "FriendMessage"
MSG_TYPE_GROUP = "GroupMessage"
MSG_TYPE_KEYWORD_FRIEND = "Friend"
MSG_TYPE_KEYWORD_GROUP = "Group"

# JSON 解析用預編譯正則
_RE_MD_CODE_BLOCK = re.compile(r"```(?:json)?\s*")
_RE_JSON_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)
_RE_JSON_ARRAY = re.compile(r"\[[^\[\]]*\]", re.DOTALL)


# ── 時間工具 ──────────────────────────────────────────────


def is_quiet_time(quiet_hours_str: str, tz: zoneinfo.ZoneInfo | None) -> bool:
    """檢查當前時間是否處於免打擾時段。支援跨日（如 ``22-6``）。"""
    try:
        start_str, end_str = quiet_hours_str.split("-")
        start_h, end_h = int(start_str), int(end_str)
    except (ValueError, TypeError, AttributeError):
        return False
    hour = (datetime.now(tz) if tz else datetime.now()).hour
    if start_h <= end_h:
        return start_h <= hour < end_h
    return hour >= start_h or hour < end_h


# ── JSON 解析 ─────────────────────────────────────────────


def parse_llm_json(
    text: str,
    *,
    expect_type: type[dict] | type[list] | None = None,
    log_tag: str = _LOG_TAG,
) -> dict | list | None:
    """從 LLM 回應文字中穩健地解析 JSON。

    處理 markdown 程式碼區塊、fallback 正則搜尋。
    *expect_type* 為 ``dict`` 時只接受物件，為 ``list`` 時只接受陣列，
    為 ``None`` 時接受任意 JSON 值。
    """
    if not text:
        return None

    # 移除 markdown 程式碼區塊標記
    cleaned = _RE_MD_CODE_BLOCK.sub("", text).strip().rstrip("`")

    # 嘗試直接解析完整文字
    try:
        result = json.loads(cleaned)
        if expect_type is not None and not isinstance(result, expect_type):
            # 型別不符，不直接回傳，繼續嘗試 fallback
            raise json.JSONDecodeError("type mismatch", cleaned, 0)
        return result
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback：以正則搜尋文字中的 JSON 片段
    patterns: list[re.Pattern[str]] = []
    if expect_type is dict or expect_type is None:
        patterns.append(_RE_JSON_OBJECT)
    if expect_type is list or expect_type is None:
        patterns.append(_RE_JSON_ARRAY)

    for pat in patterns:
        match = pat.search(cleaned)
        if match:
            try:
                result = json.loads(match.group())
                if expect_type is not None and not isinstance(result, expect_type):
                    continue
                return result
            except (json.JSONDecodeError, TypeError):
                pass

    label = "JSON 陣列" if expect_type is list else "JSON"
    logger.warning(f"{log_tag} 無法解析 LLM 的 {label} 回應: {text[:200]}")
    return None


# ── UMO 容錯包裝器 ────────────────────────────────────────

_T = TypeVar("_T")


def with_umo_fallback(
    fn: Callable[..., _T],
    session_id: str,
    *args: Any,
    **kwargs: Any,
) -> _T:
    """包裝同步函數，自動處理 UMO ValueError 並以標準三段式格式重試。"""
    try:
        return fn(session_id, *args, **kwargs)
    except ValueError as exc:
        if "too many values" not in str(exc) and "expected 3" not in str(exc):
            raise
        parsed = parse_session_id(session_id)
        if parsed is None:
            raise
        return fn(f"{parsed[0]}:{parsed[1]}:{parsed[2]}", *args, **kwargs)


async def async_with_umo_fallback(
    fn: Callable[..., Awaitable[_T]],
    session_id: str,
    *args: Any,
    **kwargs: Any,
) -> _T:
    """包裝非同步函數，自動處理 UMO ValueError 並以標準三段式格式重試。"""
    try:
        return await fn(session_id, *args, **kwargs)
    except ValueError as exc:
        if "too many values" not in str(exc) and "expected 3" not in str(exc):
            raise
        parsed = parse_session_id(session_id)
        if parsed is None:
            raise
        return await fn(f"{parsed[0]}:{parsed[1]}:{parsed[2]}", *args, **kwargs)


# ── UMO 解析 ─────────────────────────────────────────────


def parse_session_id(session_id: str) -> tuple[str, str, str] | None:
    """
    解析 AstrBot unified_msg_origin 格式。

    標準: ``平台ID:訊息類型:目標ID``
    簡寫: ``平台ID:目標ID`` → 預設 FriendMessage
    """
    if not session_id:
        return None
    parts = session_id.split(":", 2)
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], MSG_TYPE_FRIEND, parts[1]
    return None


def is_private_session(msg_type: str) -> bool:
    """判斷訊息類型是否為私聊。"""
    return any(kw in msg_type for kw in _FRIEND_KEYWORDS)


def is_group_session_id(session_id: str) -> bool:
    """快速判斷 session_id 是否為群聊（避免重複 lower 呼叫）。"""
    return "group" in session_id.lower()


# ── 日誌格式化 ────────────────────────────────────────────


def get_session_log_str(
    session_id: str,
    session_config: dict | None = None,
    session_data: dict | None = None,
) -> str:
    """生成用於日誌顯示的會話描述字串。"""
    parsed = parse_session_id(session_id)
    if not parsed:
        return f"[{session_id}]"

    _, msg_type, target_id = parsed
    type_str = "私聊" if is_private_session(msg_type) else "群聊"

    name = ""
    if session_config:
        name = session_config.get("_session_name") or session_config.get(
            "session_name", ""
        )

    if name:
        return f"[{type_str} {target_id} ({name})]"
    return f"[{type_str} {target_id}]"


# ── 平台解析 ─────────────────────────────────────────────


def resolve_full_umo(
    target_id: str,
    msg_type: str,
    platform_manager: Any,
    session_data: dict,
    preferred_platform: str | None = None,
) -> str:
    """
    動態解析並驗證存活的 UMO。

    優先使用 *preferred_platform*；其次從已知 session_data 中查找；
    最後回退到任意運行中的平台。
    """
    type_keyword = (
        MSG_TYPE_KEYWORD_FRIEND
        if is_private_session(msg_type)
        else MSG_TYPE_KEYWORD_GROUP
    )

    # 建立活躍平台索引（排除 webchat）
    active: dict[str, Platform] = {}
    for p in platform_manager.get_insts():
        pid = p.meta().id
        if pid and "webchat" not in pid.lower():
            active[pid] = p

    def _is_running(pid: str) -> bool:
        inst = active.get(pid)
        return inst is not None and inst.status == PlatformStatus.RUNNING

    # 1) 優先平台
    if preferred_platform and _is_running(preferred_platform):
        return f"{preferred_platform}:{msg_type}:{target_id}"

    # 2) 從已知 session_data 查找
    suffix = f":{target_id}"
    for existing_id in session_data:
        if type_keyword in existing_id and existing_id.endswith(suffix):
            pid = existing_id.split(":", 1)[0]
            if _is_running(pid):
                return existing_id

    # 3) 回退到任意運行中平台
    for pid, inst in active.items():
        if inst.status == PlatformStatus.RUNNING:
            return f"{pid}:{msg_type}:{target_id}"

    # 4) 最終兜底
    fallback = next(iter(active), "default")
    return f"{fallback}:{msg_type}:{target_id}"
