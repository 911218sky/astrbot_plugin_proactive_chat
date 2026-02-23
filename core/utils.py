# core/utils.py — 通用工具函數
"""免打擾判斷、UMO 解析、日誌格式化、平台解析。"""

from __future__ import annotations

import zoneinfo
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.core.platform.platform import Platform

from astrbot.core.platform.platform import PlatformStatus

# ── 常數 ──────────────────────────────────────────────────

_FRIEND_KEYWORDS = frozenset(("Friend", "Private"))
_LOG_TAG = "[主動訊息]"


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
        return parts[0], "FriendMessage", parts[1]
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
        name = session_config.get("_session_name") or session_config.get("session_name", "")

    if name:
        return f"[{type_str} {target_id} ({name})]"
    return f"[{type_str} {target_id}]"


# ── 平台解析 ─────────────────────────────────────────────

def resolve_full_umo(
    target_id: str,
    msg_type: str,
    platform_manager,
    session_data: dict,
    preferred_platform: str | None = None,
) -> str:
    """
    動態解析並驗證存活的 UMO。

    優先使用 *preferred_platform*；其次從已知 session_data 中查找；
    最後回退到任意運行中的平台。
    """
    type_keyword = "Friend" if is_private_session(msg_type) else "Group"

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
