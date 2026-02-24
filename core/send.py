# core/send.py — 主動訊息發送邏輯（文字 / TTS / 分段）
"""
將訊息發送相關的邏輯從 main.py 中抽離，包括：
- 主動訊息發送（TTS + 文字 + 分段）
- TTS provider 取得與語音發送
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain

from .config import get_session_config
from .messaging import calc_segment_interval, send_chain_with_hooks, split_text
from .utils import is_group_session_id, parse_session_id

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

_LOG_TAG = "[主動訊息]"


async def send_proactive_message(
    *,
    session_id: str,
    text: str,
    config,
    context: Context,
    session_data: dict,
    reset_group_silence_cb: Callable[[str], Awaitable[None]] | None = None,
    last_bot_message_time_setter: Callable[[float], None] | None = None,
) -> None:
    """發送主動訊息。

    流程：嘗試 TTS 語音 → 判斷是否需要文字 → 分段或整段發送 →
          群聊額外重設沉默倒計時。
    """
    session_config = get_session_config(config, session_id)
    if not session_config:
        return

    tts_conf = session_config.get("tts_settings", {})
    seg_conf = session_config.get("segmented_reply_settings", {})

    # 嘗試 TTS 發送
    is_tts_sent = False
    if tts_conf.get("enable_tts", True):
        is_tts_sent = await try_send_tts(session_id, text, context)

    # 判斷是否需要額外發送文字（TTS 失敗時一定發、成功時看 always_send_text）
    should_send_text = not is_tts_sent or tts_conf.get("always_send_text", True)
    if should_send_text:
        enable_seg = seg_conf.get("enable", False)
        threshold = seg_conf.get("words_count_threshold", 150)

        if enable_seg and len(text) <= threshold:
            segments = split_text(text, seg_conf) or [text]
            for idx, seg in enumerate(segments):
                await send_chain_with_hooks(
                    session_id, [Plain(text=seg)], context, session_data
                )
                if idx < len(segments) - 1:
                    interval = calc_segment_interval(seg, seg_conf)
                    await asyncio.sleep(interval)
        else:
            await send_chain_with_hooks(
                session_id, [Plain(text=text)], context, session_data
            )

    # 群聊：發送後重設沉默倒計時
    if is_group_session_id(session_id):
        if reset_group_silence_cb:
            await reset_group_silence_cb(session_id)
        if last_bot_message_time_setter:
            import time

            last_bot_message_time_setter(time.time())


async def try_send_tts(session_id: str, text: str, context: Context) -> bool:
    """嘗試透過 TTS 發送語音。成功回傳 True，失敗回傳 False。"""
    try:
        tts_provider = get_tts_provider(session_id, context)
        if not tts_provider:
            return False
        audio_path = await tts_provider.get_audio(text)
        if not audio_path:
            return False
        await context.send_message(session_id, MessageChain([Record(file=audio_path)]))
        await asyncio.sleep(0.5)
        return True
    except Exception as e:
        logger.error(f"{_LOG_TAG} TTS 流程異常: {e}")
        return False


def get_tts_provider(session_id: str, context: Context):
    """取得 TTS provider。

    處理 AstrBot 某些版本中 UMO 格式不相容的 ValueError，
    自動回退為標準三段式格式重試。
    """
    try:
        return context.get_using_tts_provider(umo=session_id)
    except ValueError as e:
        if "too many values" not in str(e) and "expected 3" not in str(e):
            raise
        parsed = parse_session_id(session_id)
        if parsed:
            return context.get_using_tts_provider(
                umo=f"{parsed[0]}:{parsed[1]}:{parsed[2]}"
            )
        return None
