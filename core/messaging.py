# core/messaging.py — 訊息發送
"""裝飾鉤子、分段回覆、歷史記錄清洗。"""

from __future__ import annotations

import math
import random
import re
import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.astrbot_message import (
    AstrBotMessage,
    Group,
    MessageMember,
)
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform import PlatformStatus
from astrbot.core.star.star_handler import EventType, star_handlers_registry

try:
    from astrbot.core.platform.astr_message_event import MessageSession as MS
except ImportError:
    from astrbot.core.platform.message_session import MessageSession as MS

from .utils import parse_session_id

_LOG_TAG = "[主動訊息]"

# 預編譯預設分段正則
_DEFAULT_SPLIT_RE = re.compile(r".*?[。？！~…\n]+|.+$")


# ── 裝飾鉤子 ─────────────────────────────────────────────


async def trigger_decorating_hooks(
    session_id: str,
    chain: list,
    context,
    session_data: dict,
) -> list:
    """觸發 ``OnDecoratingResultEvent``，讓其他插件有機會處理訊息。"""
    parsed = parse_session_id(session_id)
    if not parsed:
        return chain

    platform_name, msg_type_str, target_id = parsed

    # 查找平台實例（先 id 後 name）
    platform_inst = None
    for p in context.platform_manager.platform_insts:
        meta = p.meta()
        if meta.id == platform_name or meta.name == platform_name:
            platform_inst = p
            break
    if not platform_inst:
        return chain

    # 構建模擬事件
    is_group = "Group" in msg_type_str
    msg_obj = AstrBotMessage()
    msg_obj.type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
    if is_group:
        msg_obj.group = Group(group_id=target_id)
    msg_obj.session_id = target_id
    msg_obj.message = chain
    msg_obj.self_id = session_data.get(session_id, {}).get("self_id", "bot")
    msg_obj.sender = MessageMember(user_id=target_id)
    msg_obj.message_str = ""
    msg_obj.raw_message = None
    msg_obj.message_id = ""

    event = AstrMessageEvent(
        message_str="",
        message_obj=msg_obj,
        platform_meta=platform_inst.meta(),
        session_id=target_id,
    )
    res = MessageEventResult()
    res.chain = chain
    event.set_result(res)

    for handler in star_handlers_registry.get_handlers_by_event_type(
        EventType.OnDecoratingResultEvent
    ):
        try:
            await handler.handler(event)
        except Exception as e:
            etype = type(e).__name__
            logger.error(
                f"{_LOG_TAG} 裝飾鉤子執行失敗 | 來源: {handler.handler_full_name}, "
                f"類型: {etype}, 詳情: {e}"
            )
            if "Available" in etype:
                logger.error(
                    f"{_LOG_TAG} 疑似 ApiNotAvailable 來源: {handler.handler_module_path}"
                )

    final = event.get_result()
    return (final.chain or []) if final is not None else chain


# ── 訊息發送 ─────────────────────────────────────────────


async def send_chain_with_hooks(
    session_id: str,
    components: list,
    context,
    session_data: dict,
) -> None:
    """經裝飾鉤子處理後，透過指定平台發送訊息鏈。"""
    processed = await trigger_decorating_hooks(
        session_id, components, context, session_data
    )
    if not processed:
        return

    chain = MessageChain(processed)
    parsed = parse_session_id(session_id)
    if not parsed:
        await context.send_message(session_id, chain)
        return

    p_id, m_type_str, t_id = parsed
    m_type = (
        MessageType.GROUP_MESSAGE
        if "Group" in m_type_str
        else MessageType.FRIEND_MESSAGE
    )

    target = None
    for p in context.platform_manager.get_insts():
        if p.meta().id == p_id:
            target = p
            break

    if not target:
        logger.warning(f"{_LOG_TAG} 找不到平台 {p_id}，使用核心 API 兜底。")
        await context.send_message(session_id, chain)
        return

    if target.status != PlatformStatus.RUNNING:
        logger.warning(f"{_LOG_TAG} 平台 {p_id} 未運行，跳過發送。")
        return

    try:
        await target.send_by_session(
            MS(platform_name=p_id, message_type=m_type, session_id=t_id), chain
        )
        logger.debug(f"{_LOG_TAG} 訊息已透過平台 {p_id} 送達")
    except Exception as e:
        logger.error(f"{_LOG_TAG} 透過平台 {p_id} 發送失敗: {e}")
        logger.debug(traceback.format_exc())


# ── 文本分段 ─────────────────────────────────────────────


def split_text(text: str, settings: dict) -> list[str]:
    """根據配置將文本分段。"""
    mode = settings.get("split_mode", "regex")

    if mode == "regex":
        pattern_str = settings.get("regex", "")
        try:
            pat = re.compile(pattern_str) if pattern_str else _DEFAULT_SPLIT_RE
            segments = pat.findall(text)
        except re.error as e:
            logger.warning(f"{_LOG_TAG} 分段正則錯誤: {e}，回退整段發送。")
            return [text]
        return [s.strip() for s in segments if s.strip()] or [text]

    # words 模式
    split_chars = set(settings.get("split_words", ["。", "？", "！", "~", "…"]))
    segments: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in split_chars:
            s = "".join(buf).strip()
            if s:
                segments.append(s)
            buf.clear()
    if buf:
        s = "".join(buf).strip()
        if s:
            segments.append(s)
    return segments or [text]


def calc_segment_interval(text: str, settings: dict) -> float:
    """計算分段回覆的間隔時間（秒）。"""
    if settings.get("interval_method") == "log":
        base = float(settings.get("log_base", 1.8))
        # ASCII → 按空格分詞；非 ASCII → 按字元計數
        n = len(text.split()) if text.isascii() else sum(c.isalnum() for c in text)
        val = math.log(n + 1, base)
        return random.uniform(val, val + 0.5)

    # random 模式
    raw = settings.get("interval", "1.5,3.5")
    try:
        parts = [float(x) for x in raw.replace(" ", "").split(",")]
        lo, hi = (parts[0], parts[1]) if len(parts) == 2 else (1.5, 3.5)
    except (ValueError, IndexError):
        lo, hi = 1.5, 3.5
    return random.uniform(lo, hi)


# ── 歷史清洗 ─────────────────────────────────────────────


def sanitize_history_content(history: list) -> list:
    """清洗歷史記錄，確保 content 欄位格式一致。"""
    if not history:
        return []

    result: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            result.append(item)
            continue
        entry = item.copy()
        content = entry.get("content")
        if isinstance(content, list):
            entry["content"] = [
                p
                if isinstance(p, dict)
                else {"type": "text", "text": p if isinstance(p, str) else str(p)}
                for p in content
            ]
        elif content is not None and not isinstance(content, str):
            entry["content"] = str(content)
        result.append(entry)
    return result
