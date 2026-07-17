# core/send.py — 主動訊息發送邏輯（文字 / TTS / 分段）
"""
將訊息發送相關的邏輯從 main.py 中抽離，包括：
- 主動訊息發送（TTS + 文字 + 分段）
- TTS provider 取得與語音發送
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain

from .config import get_session_config
from .delivery import (
    AcceptedComponent,
    AcceptedComponentKind,
    AcceptedTurn,
    DispatchStatus,
    GateVerdict,
    make_accepted_turn,
)
from .messaging import (
    calc_segment_interval,
    sanitize_outgoing_text,
    send_chain_with_hooks,
    split_text,
)
from .utils import is_group_session_id, with_umo_fallback

if TYPE_CHECKING:
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from astrbot.core.star.context import Context

_LOG_TAG = "[主動訊息]"


async def send_proactive_message(
    *,
    session_id: str,
    text: str,
    config: AstrBotConfig,
    context: Context,
    session_data: dict,
    reset_group_silence_cb: Callable[[str], Awaitable[None]] | None = None,
    last_bot_message_time_setter: Callable[[float], None] | None = None,
) -> bool:
    turn = await dispatch_proactive_message(
        session_id=session_id,
        text=text,
        config=config,
        context=context,
        session_data=session_data,
        reset_group_silence_cb=reset_group_silence_cb,
        last_bot_message_time_setter=last_bot_message_time_setter,
    )
    return turn.status is not DispatchStatus.FAILED


async def dispatch_proactive_message(
    *,
    session_id: str,
    text: str,
    config: AstrBotConfig,
    context: Context,
    session_data: dict,
    gate_check: Callable[[], GateVerdict] | None = None,
    reset_group_silence_cb: Callable[[str], Awaitable[None]] | None = None,
    last_bot_message_time_setter: Callable[[float], None] | None = None,
) -> AcceptedTurn:
    def verdict() -> GateVerdict:
        return gate_check() if gate_check else GateVerdict.CURRENT

    accepted: list[AcceptedComponent] = []
    intended = 1
    current = verdict()
    if current is not GateVerdict.CURRENT:
        return make_accepted_turn(text, (), intended_components=1, verdict=current)

    session_config = get_session_config(config, session_id)
    if not session_config:
        return make_accepted_turn(text, (), intended_components=1)

    text = sanitize_outgoing_text(text)
    if not text:
        logger.warning(
            f"{_LOG_TAG} 主動訊息清理後為空，取消發送 | session={session_id}"
        )
        return make_accepted_turn(text, (), intended_components=1)

    tts_conf = session_config.get("tts_settings", {})
    seg_conf = session_config.get("segmented_reply_settings", {})
    text_components = (
        split_text(text, seg_conf) or [text]
        if seg_conf.get("enable", False)
        else [text]
    )
    intended = 0
    tts_accepted = False

    if tts_conf.get("enable_tts", True):
        current = verdict()
        if current is not GateVerdict.CURRENT:
            return make_accepted_turn(text, (), intended_components=1, verdict=current)
        provider = get_tts_provider(session_id, context)
        if provider:
            intended = 1
            try:
                audio_path = await provider.get_audio(text)
            except (OSError, ValueError) as error:
                logger.error(
                    f"{_LOG_TAG} TTS provider 異常 | session={session_id}: {error}"
                )
                audio_path = ""
            current = verdict()
            if current is not GateVerdict.CURRENT:
                return make_accepted_turn(
                    text, (), intended_components=1, verdict=current
                )
            if audio_path:
                try:
                    tts_accepted = bool(
                        await context.send_message(
                            session_id, MessageChain([Record(file=audio_path)])
                        )
                    )
                except (OSError, ValueError) as error:
                    logger.error(
                        f"{_LOG_TAG} TTS 元件派送異常 | session={session_id}: {error}"
                    )
                if tts_accepted:
                    accepted.append(
                        AcceptedComponent(AcceptedComponentKind.TTS, audio_path)
                    )
                    intended = 1 + (
                        len(text_components)
                        if tts_conf.get("always_send_text", True)
                        else 0
                    )
                    await asyncio.sleep(0.5)
                    current = verdict()
                    if current is not GateVerdict.CURRENT:
                        return make_accepted_turn(
                            text,
                            tuple(accepted),
                            intended_components=intended,
                            verdict=current,
                        )

    should_send_text = not tts_accepted or tts_conf.get("always_send_text", True)
    if should_send_text:
        if not tts_accepted:
            intended += len(text_components)
        for index, segment in enumerate(text_components):
            current = verdict()
            if current is not GateVerdict.CURRENT:
                return make_accepted_turn(
                    text,
                    tuple(accepted),
                    intended_components=intended,
                    verdict=current,
                )
            component_accepted = await send_chain_with_hooks(
                session_id,
                [Plain(text=segment)],
                context,
                session_data,
                gate_check,
            )
            current = verdict()
            if not component_accepted:
                return make_accepted_turn(
                    text,
                    tuple(accepted),
                    intended_components=intended,
                    verdict=current,
                )
            accepted.append(AcceptedComponent(AcceptedComponentKind.TEXT, segment))
            if index < len(text_components) - 1:
                await asyncio.sleep(calc_segment_interval(segment, seg_conf))
                current = verdict()
                if current is not GateVerdict.CURRENT:
                    return make_accepted_turn(
                        text,
                        tuple(accepted),
                        intended_components=intended,
                        verdict=current,
                    )

    if accepted and is_group_session_id(session_id):
        current = verdict()
        if current is not GateVerdict.CURRENT:
            return make_accepted_turn(
                text,
                tuple(accepted),
                intended_components=intended,
                verdict=current,
            )
        if reset_group_silence_cb:
            await reset_group_silence_cb(session_id)
        if last_bot_message_time_setter:
            last_bot_message_time_setter(time.time())
    return make_accepted_turn(
        text,
        tuple(accepted),
        intended_components=intended,
        verdict=verdict(),
    )


async def try_send_tts(session_id: str, text: str, context: Context) -> bool:
    """嘗試透過 TTS 發送語音。成功回傳 True，失敗回傳 False。"""
    try:
        tts_provider = get_tts_provider(session_id, context)
        if not tts_provider:
            return False
        audio_path = await tts_provider.get_audio(text)
        if not audio_path:
            return False
        ok = await context.send_message(
            session_id, MessageChain([Record(file=audio_path)])
        )
        if not ok:
            logger.warning(f"{_LOG_TAG} TTS 語音發送失敗 | session={session_id}")
            return False
        await asyncio.sleep(0.5)
        return True
    except Exception as e:
        logger.error(
            f"{_LOG_TAG} try_send_tts TTS 流程異常 | session={session_id}: {e}"
        )
        return False


def get_tts_provider(session_id: str, context: Context):
    """取得 TTS provider。

    處理 AstrBot 某些版本中 UMO 格式不相容的 ValueError，
    自動回退為標準三段式格式重試。
    """
    try:
        return with_umo_fallback(
            lambda sid: context.get_using_tts_provider(umo=sid),
            session_id,
        )
    except ValueError:
        return None
