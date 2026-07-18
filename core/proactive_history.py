from __future__ import annotations

import asyncio
import traceback
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import uuid4

import anyio
from astrbot.api import logger
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.db.po import ConversationV2
from sqlmodel import col, select, text, update

from .delivery import AcceptedTurn, DispatchGate, GateVerdict, accepted_turn_text

if TYPE_CHECKING:
    from ..main import ProactiveChatPlugin

Sleep = Callable[[float], Awaitable[None]]
WritePair = Callable[..., Awaitable[bool]]
SettleMarker = Callable[..., Awaitable[None]]

_LOG_TAG = "[主動訊息]"
_MARKER_KEY = "_astrbot_proactive_history_entry_id"
_SQLITE_LOCK_KEYWORDS = frozenset({"database is locked", "database table is locked"})


def _allows_history(plugin: ProactiveChatPlugin, gate: DispatchGate) -> bool:
    return plugin._gate_verdict(gate) in (
        GateVerdict.CURRENT,
        GateVerdict.QUIET_HOURS,
    )


def _coerce_float(value, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _history_settings(plugin: ProactiveChatPlugin, session_config: dict) -> tuple:
    defaults = (False, 2.0, 3)
    global_settings = plugin.config.get("history_settings", {})
    session_settings = session_config.get("history_settings", {})
    if not isinstance(global_settings, dict):
        global_settings = {}
    if not isinstance(session_settings, dict):
        session_settings = {}
    merged = {
        **{
            "save_proactive_history": defaults[0],
            "history_save_delay_seconds": defaults[1],
            "history_save_retry_attempts": defaults[2],
        },
        **{key: value for key, value in global_settings.items() if value is not None},
        **{key: value for key, value in session_settings.items() if value is not None},
    }
    return (
        bool(merged.get("save_proactive_history", False)),
        _coerce_float(merged.get("history_save_delay_seconds"), 2.0, 0.0, 30.0),
        int(_coerce_float(merged.get("history_save_retry_attempts"), 3, 0.0, 10.0)),
    )


async def save_conversation_history(
    plugin: ProactiveChatPlugin,
    session_config: dict,
    conv_id: str,
    user_prompt: str,
    assistant_response: str | tuple[AcceptedTurn, ...],
    gate: DispatchGate,
    *,
    sleep: Sleep,
    write_pair: WritePair,
) -> bool:
    enabled, delay_seconds, retry_attempts = _history_settings(plugin, session_config)
    if not enabled or not conv_id or not _allows_history(plugin, gate):
        return False
    if delay_seconds > 0:
        await sleep(delay_seconds)
        if not _allows_history(plugin, gate):
            return False
    async with plugin._history_save_lock:
        for attempt in range(1, retry_attempts + 2):
            if not _allows_history(plugin, gate):
                return False
            try:
                return await write_pair(
                    plugin, conv_id, user_prompt, assistant_response, gate
                )
            except Exception as error:
                error_text = "".join(traceback.format_exception(error)).lower()
                locked = any(word in error_text for word in _SQLITE_LOCK_KEYWORDS)
                if not locked:
                    logger.warning(f"{_LOG_TAG} 主動訊息寫回歷史失敗，已跳過: {error}")
                    return False
                if attempt > retry_attempts:
                    logger.warning(f"{_LOG_TAG} 對話歷史忙碌，已跳過本次寫回")
                    return False
                await sleep(min(0.5 * attempt, 2.0))
    return False


async def write_guarded_history_pair(
    plugin: ProactiveChatPlugin,
    conv_id: str,
    user_prompt: str,
    assistant_response: str | tuple[AcceptedTurn, ...],
    gate: DispatchGate,
    *,
    settle_marker: SettleMarker,
) -> bool:
    manager = plugin.context.conversation_manager
    marker = uuid4().hex
    user_message, assistant_message = marked_history_pair(
        user_prompt, assistant_response, marker
    )
    revision_signal = plugin._delivery_coordinators.revision_signal(gate)
    watcher_ready = anyio.Event()
    activity_changed = False
    logical_success = False
    write_task: asyncio.Task[None] | None = None

    async def cancel_on_activity() -> None:
        nonlocal activity_changed, write_task
        watcher_ready.set()
        await revision_signal.wait()
        if plugin._gate_verdict(gate) is GateVerdict.ACTIVITY_CHANGED:
            activity_changed = True
            if write_task is not None:
                write_task.cancel()

    cancelled_error = anyio.get_cancelled_exc_class()
    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(cancel_on_activity)
            await watcher_ready.wait()
            if not _allows_history(plugin, gate):
                task_group.cancel_scope.cancel()
            else:
                write_task = asyncio.create_task(
                    manager.add_message_pair(
                        cid=conv_id,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
                )
                await write_task
                task_group.cancel_scope.cancel()
        if activity_changed or not _allows_history(plugin, gate):
            await settle_marker(manager, conv_id, marker, remove_pair=True)
            return False
        logical_success = True
        await settle_marker(manager, conv_id, marker, remove_pair=False)
        return True
    except cancelled_error:
        current = asyncio.current_task()
        externally_cancelled = bool(current and current.cancelling())
        await settle_marker(manager, conv_id, marker, remove_pair=not logical_success)
        if activity_changed and not externally_cancelled:
            return False
        raise
    except Exception:
        await settle_marker(manager, conv_id, marker, remove_pair=True)
        raise


def marked_history_pair(
    user_prompt: str,
    assistant_response: str | tuple[AcceptedTurn, ...],
    marker: str,
) -> tuple[dict, dict]:
    if isinstance(assistant_response, str):
        texts = (assistant_response,)
    else:
        texts = tuple(
            text for turn in assistant_response if (text := accepted_turn_text(turn))
        )
    return (
        {
            "role": "user",
            "content": [{"type": "text", "text": user_prompt}],
            _MARKER_KEY: marker,
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": value} for value in texts],
            _MARKER_KEY: marker,
        },
    )


async def settle_history_marker_shielded(
    manager: ConversationManager,
    conv_id: str,
    marker: str,
    *,
    remove_pair: bool,
) -> None:
    with anyio.CancelScope(shield=True):
        await _settle_history_marker(manager, conv_id, marker, remove_pair=remove_pair)


async def _settle_history_marker(
    manager: ConversationManager,
    conv_id: str,
    marker: str,
    *,
    remove_pair: bool,
) -> None:
    async with manager.db.get_db() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        try:
            result = await session.execute(
                select(ConversationV2.content).where(
                    col(ConversationV2.conversation_id) == conv_id
                )
            )
            history = result.scalar_one_or_none()
            if not isinstance(history, list):
                await session.commit()
                return
            settled = []
            changed = False
            for message in history:
                if not isinstance(message, dict) or message.get(_MARKER_KEY) != marker:
                    settled.append(message)
                    continue
                changed = True
                if not remove_pair:
                    cleaned = dict(message)
                    cleaned.pop(_MARKER_KEY, None)
                    settled.append(cleaned)
            if changed:
                await session.execute(
                    update(ConversationV2)
                    .where(col(ConversationV2.conversation_id) == conv_id)
                    .values(content=settled)
                )
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
