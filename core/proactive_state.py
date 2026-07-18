from __future__ import annotations

import time
import traceback
import zoneinfo
from datetime import datetime
from typing import TYPE_CHECKING, assert_never

from astrbot.api import logger

from .auto_check import (
    clamp_auto_check_interval,
    compute_session_interval,
    resolve_auto_check_settings,
)
from .config import get_session_config
from .delivery import DispatchGate, GateVerdict
from .immediate_follow_up import resolve_immediate_follow_up_settings
from .human_like import (
    apply_heat,
    cooldown_is_active,
    delivery_counts,
    is_outreach_capped,
    normalize_cooldown_until,
    normalize_heat_score,
    resolve_human_like_settings,
    should_enter_cooldown,
)
from .scheduler import (
    is_unanswered_limit_reached,
    should_trigger_by_unanswered,
)
from .utils import (
    get_session_log_str,
    is_group_session_id,
    is_private_session,
    parse_session_id,
)

if TYPE_CHECKING:
    from ..main import ProactiveChatPlugin

_LOG_TAG = "[主動訊息]"
_AUTH_ERROR_KEYWORDS = frozenset(
    {"authentication", "auth", "unauthorized", "forbidden"}
)


def is_habit_job(job_id: str) -> bool:
    return bool(job_id and job_id.startswith("habit_"))


def find_context_task(
    plugin: ProactiveChatPlugin, session_id: str, ctx_job_id: str
) -> dict | None:
    if not ctx_job_id:
        return None
    task_list = plugin._pending_context_tasks.get(session_id, [])
    return next((task for task in task_list if task.get("job_id") == ctx_job_id), None)


def active_task_description(plugin: ProactiveChatPlugin, session_id: str) -> str:
    session_info = plugin.session_data.get(session_id, {})
    if not isinstance(session_info, dict):
        return ""
    for key in (
        "task_description",
        "auto_trigger_description",
        "group_idle_description",
    ):
        description = str(session_info.get(key) or "").strip()
        if description:
            return description
    return ""


def _coerce_timestamp(value) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return timestamp if 0 < timestamp <= time.time() + 60 else None


def format_last_reply_time(timestamp: float, timezone: zoneinfo.ZoneInfo | None) -> str:
    if timestamp <= 0:
        return "未知"
    elapsed_minutes = int(time.time() - timestamp) // 60
    if elapsed_minutes < 60:
        elapsed = f"{elapsed_minutes}分鐘"
    else:
        hours, minutes = divmod(elapsed_minutes, 60)
        elapsed = f"{hours}小時{minutes}分鐘" if minutes else f"{hours}小時"
    value = datetime.fromtimestamp(timestamp, tz=timezone)
    return f"{value.strftime('%Y年%m月%d日 %H:%M')}（{elapsed}前）"


def format_first_interaction_time(value, timezone: zoneinfo.ZoneInfo | None) -> str:
    timestamp = _coerce_timestamp(value)
    if timestamp is None:
        return "未知"
    return datetime.fromtimestamp(timestamp, tz=timezone).strftime("%Y年%m月%d日 %H:%M")


def format_elapsed_duration(value) -> str:
    timestamp = _coerce_timestamp(value)
    if timestamp is None:
        return "未知"
    minutes = max(0, int(time.time() - timestamp)) // 60
    if minutes < 1:
        return "不到1分鐘"
    if minutes < 60:
        return f"{minutes}分鐘"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小時{minutes}分鐘" if minutes else f"{hours}小時"
    days, hours = divmod(hours, 24)
    if days < 30:
        return f"{days}天{hours}小時" if hours else f"{days}天"
    months, days = divmod(days, 30)
    if months < 12:
        return f"{months}個月{days}天" if days else f"{months}個月"
    years, months = divmod(months, 12)
    return f"{years}年{months}個月" if months else f"{years}年"


async def check_preconditions(
    plugin: ProactiveChatPlugin, session_id: str, *, skip_unanswered: bool = False
) -> tuple[dict | None, int, bool]:
    session_config = get_session_config(plugin.config, session_id)
    if not await plugin._is_chat_allowed(session_id, session_config):
        await plugin._schedule_next_chat_and_save(session_id)
        return None, 0, False

    schedule_conf = session_config.get("schedule_settings", {})
    log_str = get_session_log_str(session_id, session_config, plugin.session_data)
    human_blocked = False
    async with plugin.data_lock:
        state = plugin.session_data.get(session_id, {})
        unanswered_count = state.get("unanswered_count", 0)
        human_settings = resolve_human_like_settings(session_config)
        follow_up_enabled = resolve_immediate_follow_up_settings(session_config).enable
        parsed_session = parse_session_id(session_id)
        if (
            (human_settings.enable or follow_up_enabled)
            and parsed_session
            and is_private_session(parsed_session[1])
        ):
            now = time.time()
            cooldown_until = normalize_cooldown_until(
                state.get("human_like_cooldown_until")
            )
            sent_hour, sent_day, normalized = delivery_counts(
                state.get("human_like_delivery_timestamps"),
                now,
                plugin.timezone,
            )
            if normalized != state.get("human_like_delivery_timestamps"):
                state["human_like_delivery_timestamps"] = normalized
                await plugin._save_data()
            if cooldown_is_active(cooldown_until, now) or is_outreach_capped(
                human_settings, sent_hour, sent_day, int(unanswered_count or 0)
            ):
                human_blocked = True
                logger.info(
                    f"{_LOG_TAG} {log_str} 人性化冷卻或頻率上限生效，略過本次 LLM。"
                )
        if human_blocked:
            should_trigger, reason = False, "人性化冷卻或頻率上限"
        elif skip_unanswered:
            reached, reason = is_unanswered_limit_reached(
                unanswered_count, schedule_conf, plugin.timezone
            )
            should_trigger = not reached
        else:
            should_trigger, reason = should_trigger_by_unanswered(
                unanswered_count, schedule_conf, plugin.timezone
            )
    if not should_trigger:
        logger.info(f"{_LOG_TAG} {log_str} {reason}")
        if human_blocked:
            await plugin._schedule_next_chat_and_save(session_id)
        elif "衰減" in reason:
            await plugin._schedule_next_chat_and_save(session_id)
        elif "硬性上限" in reason:
            await plugin._clear_regular_job_state(session_id)
            return None, 0, True
        return None, 0, False
    if reason:
        logger.info(f"{_LOG_TAG} {log_str} {reason}")
    return session_config, unanswered_count, False


async def clear_regular_job_state_if_current(
    plugin: ProactiveChatPlugin, session_id: str, gate: DispatchGate
) -> bool:
    async with plugin.data_lock:
        if plugin._gate_verdict(gate) is not GateVerdict.CURRENT:
            return False
        session_state = plugin.session_data.get(session_id)
        if not isinstance(session_state, dict):
            return True
        if session_state.pop("next_trigger_time", None) is not None:
            await plugin._save_data()
        return True


async def update_unanswered_and_reschedule(
    plugin: ProactiveChatPlugin,
    session_id: str,
    session_config: dict,
    unanswered_count: int,
    *,
    ctx_job_id: str = "",
    clear_task_description: bool = False,
    next_check_minutes: int | None = None,
    gate: DispatchGate,
) -> bool:
    habit_task = plugin._find_habit_task(session_id, ctx_job_id)
    count_unanswered = not habit_task or bool(habit_task.get("count_unanswered", False))
    async with plugin.data_lock:
        match plugin._gate_verdict(gate):
            case GateVerdict.CURRENT | GateVerdict.QUIET_HOURS:
                pass
            case GateVerdict.ACTIVITY_CHANGED | GateVerdict.DISABLED:
                return False
            case unreachable:
                assert_never(unreachable)
        state = plugin.session_data.setdefault(session_id, {})
        next_count = unanswered_count + int(count_unanswered)
        state["unanswered_count"] = next_count
        human_settings = resolve_human_like_settings(session_config)
        follow_up_enabled = resolve_immediate_follow_up_settings(session_config).enable
        parsed_session = parse_session_id(session_id)
        if (
            (human_settings.enable or follow_up_enabled)
            and parsed_session
            and is_private_session(parsed_session[1])
        ):
            now = time.time()
            state["interaction_heat"] = apply_heat(
                normalize_heat_score(
                    state.get("interaction_heat"),
                    human_settings.initial_heat_score,
                ),
                "proactive_delivery",
                human_settings,
            )
            sent = state.get("human_like_delivery_timestamps")
            timestamps = sent if isinstance(sent, list) else []
            timestamps.append(now)
            state["human_like_delivery_timestamps"] = timestamps
            if should_enter_cooldown(next_count, human_settings):
                state["human_like_cooldown_until"] = now + (
                    human_settings.cooldown_minutes * 60
                )
        if clear_task_description:
            state.pop("task_description", None)
        if habit_task and not count_unanswered:
            await plugin._save_data()
            return True
        if is_group_session_id(session_id):
            state.pop("next_trigger_time", None)
            await plugin._save_data()
            return True
        schedule_conf = session_config.get("schedule_settings", {})
        reached, reason = is_unanswered_limit_reached(
            next_count, schedule_conf, plugin.timezone
        )
        if reached:
            state.pop("next_trigger_time", None)
            await plugin._save_data()
            logger.info(f"{_LOG_TAG} {reason}，不再安排下一次主動訊息。")
            return True
        if next_check_minutes is not None:
            auto_settings = resolve_auto_check_settings(session_config)
            interval = clamp_auto_check_interval(
                int(next_check_minutes) * 60, auto_settings
            )
        else:
            interval = compute_session_interval(
                schedule_conf,
                session_config,
                plugin.timezone,
                next_count,
            )
        run_date = datetime.fromtimestamp(time.time() + interval, tz=plugin.timezone)
        state["next_trigger_time"] = run_date.timestamp()
        await plugin._save_data()
        plugin._add_scheduled_job_at(session_id, run_date)
        return True


async def cleanup_context_task(
    plugin: ProactiveChatPlugin, session_id: str, ctx_job_id: str
) -> None:
    if session_id not in plugin._pending_context_tasks:
        return
    tasks = plugin._pending_context_tasks[session_id]
    plugin._pending_context_tasks[session_id] = [
        task for task in tasks if task.get("job_id") != ctx_job_id
    ]
    if not plugin._pending_context_tasks[session_id]:
        plugin._pending_context_tasks.pop(session_id, None)
    async with plugin.data_lock:
        state = plugin.session_data.get(session_id)
        if state:
            remaining = plugin._pending_context_tasks.get(session_id)
            if remaining:
                state["pending_context_tasks"] = remaining
            else:
                state.pop("pending_context_tasks", None)
                state.pop("pending_context_task", None)
            await plugin._save_data()


async def handle_fatal_error(
    plugin: ProactiveChatPlugin,
    session_id: str,
    error: Exception,
    *,
    skip_reschedule: bool = False,
) -> None:
    logger.error(
        f"{_LOG_TAG} check_and_chat 致命錯誤 | session={session_id}: "
        f"{type(error).__name__}: {error}"
    )
    logger.debug(traceback.format_exc())
    error_text = f"{type(error).__name__} {error}".lower()
    if skip_reschedule or any(word in error_text for word in _AUTH_ERROR_KEYWORDS):
        return
    try:
        async with plugin.data_lock:
            state = plugin.session_data.get(session_id)
            if state and "next_trigger_time" in state:
                del state["next_trigger_time"]
                await plugin._save_data()
        await plugin._schedule_next_chat_and_save(session_id)
    except Exception as recovery_error:
        logger.error(f"{_LOG_TAG} 錯誤恢復中重新調度失敗: {recovery_error}")


async def reschedule_quiet_source(
    plugin: ProactiveChatPlugin, session_id: str, ctx_job_id: str
) -> None:
    await plugin._retry_chat_job(session_id, ctx_job_id)
