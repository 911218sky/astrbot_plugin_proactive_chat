from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.platform.platform import PlatformStatus

from . import (
    auto_check,
    immediate_follow_up,
    proactive_history,
    proactive_prompt,
    proactive_state,
)
from .delivery import AcceptedTurn, DispatchGate, GateVerdict
from .send import dispatch_proactive_message
from .utils import is_private_session, parse_session_id, resolve_full_umo

if TYPE_CHECKING:
    from astrbot.core.conversation_mgr import ConversationManager

    from ..main import ProactiveChatPlugin


async def check_and_chat(
    plugin: ProactiveChatPlugin,
    session_id: str,
    ctx_job_id: str = "",
    *,
    gate: DispatchGate | None = None,
) -> None:
    context_finished = False
    habit_finished = False
    limit_reached = False
    try:
        current_gate = gate or plugin._delivery_coordinators.snapshot(session_id)
        if plugin._gate_verdict(current_gate) is not GateVerdict.CURRENT:
            return
        habit_task = plugin._find_habit_task(session_id, ctx_job_id)
        skip_unanswered = bool(
            habit_task and not habit_task.get("count_unanswered", False)
        )
        session_config, unanswered_count, limit_reached = await _check_preconditions(
            plugin, session_id, skip_unanswered=skip_unanswered
        )
        if session_config is None:
            return
        if plugin._gate_verdict(current_gate) is not GateVerdict.CURRENT:
            return
        parsed_session = parse_session_id(session_id)
        is_private = bool(parsed_session and is_private_session(parsed_session[1]))
        auto_settings = auto_check.resolve_auto_check_settings(session_config)
        if is_private and auto_settings.enable and (not ctx_job_id or habit_task):
            auto_result = await _prepare_and_call_auto_check(
                plugin, session_id, session_config, unanswered_count, ctx_job_id
            )
            if auto_result is None:
                if (
                    not habit_task
                    and plugin._gate_verdict(current_gate) is GateVerdict.CURRENT
                ):
                    await plugin._schedule_next_chat_and_save(session_id)
                return
            decision, conv_id, final_prompt, context_task = auto_result
            if not decision.send_message:
                logger.info("[主動訊息] 自動查看判斷目前不需要發送，已安排下一次回訪。")
                if (
                    not habit_task
                    and plugin._gate_verdict(current_gate) is GateVerdict.CURRENT
                ):
                    if decision.next_check_minutes is None:
                        await plugin._schedule_next_chat_and_save(session_id)
                    else:
                        await plugin._schedule_next_chat_and_save(
                            session_id, delay_minutes=decision.next_check_minutes
                        )
                return
            llm_result = (
                decision.message,
                conv_id,
                final_prompt,
                context_task,
                decision.next_check_minutes,
            )
        else:
            llm_result = await _prepare_and_call_llm(
                plugin, session_id, session_config, unanswered_count, ctx_job_id
            )
        if plugin._gate_verdict(current_gate) is not GateVerdict.CURRENT:
            return
        if llm_result is None:
            return
        response_text, conv_id, final_prompt, _context_task, next_check_minutes = (
            (*llm_result, None) if len(llm_result) == 4 else llm_result
        )
        delivery_options = (
            {"next_check_minutes": next_check_minutes}
            if next_check_minutes is not None
            else {}
        )
        delivered = await _deliver_and_finalize(
            plugin,
            session_id,
            session_config,
            response_text,
            conv_id,
            final_prompt,
            unanswered_count,
            ctx_job_id,
            current_gate,
            **delivery_options,
        )
        if delivered:
            context_finished = True
            habit_finished = True
    except Exception as error:  # noqa: BLE001
        await _handle_fatal_error(
            plugin,
            session_id,
            error,
            skip_reschedule=_is_habit_job(ctx_job_id),
        )
    finally:
        if ctx_job_id and not context_finished and not _is_habit_job(ctx_job_id):
            await _cleanup_context_task(plugin, session_id, ctx_job_id)
        if ctx_job_id and _is_habit_job(ctx_job_id):
            if not habit_finished:
                await plugin._cleanup_habit_task(session_id, ctx_job_id)
            if not limit_reached:
                await plugin._schedule_next_habit_task(session_id)
            else:
                logger.info("[主動訊息] 未回覆已達硬性上限，暫停下一次習慣時段任務。")


def resolve_session_umo(plugin: ProactiveChatPlugin, session_id: str) -> str | None:
    parsed = parse_session_id(session_id)
    if not parsed:
        return session_id
    original_platform, message_type, target_id = parsed
    resolved = resolve_full_umo(
        target_id,
        message_type,
        plugin.context.platform_manager,
        plugin.session_data,
        original_platform,
    )
    resolved_parts = parse_session_id(resolved)
    if not resolved_parts:
        return resolved
    running = {
        platform.meta().id: platform
        for platform in plugin.context.platform_manager.get_insts()
        if platform.meta().id
    }
    platform = running.get(resolved_parts[0])
    if platform is None or platform.status != PlatformStatus.RUNNING:
        return None
    return resolved


async def _check_preconditions(plugin, session_id: str, **kwargs):
    return await proactive_state.check_preconditions(plugin, session_id, **kwargs)


async def _prepare_and_call_llm(plugin, *args):
    return await proactive_prompt.prepare_and_call_llm(plugin, *args)


async def _prepare_and_call_auto_check(plugin, *args):
    return await proactive_prompt.prepare_and_call_auto_check(plugin, *args)


async def _deliver_and_finalize(
    plugin, *args, random_source=random.random, next_check_minutes=None
):
    return await immediate_follow_up.deliver_and_finalize(
        plugin,
        *args,
        dispatch=dispatch_proactive_message,
        controller=_request_follow_up_decision,
        message_controller=_request_follow_up_message,
        finalize=_update_unanswered_and_reschedule,
        save_history=_save_conversation_history,
        cleanup_context=_cleanup_context_task,
        clear_failed=_clear_regular_job_state_if_current,
        reschedule_quiet=_reschedule_quiet_source,
        is_habit_job=_is_habit_job,
        sleep=asyncio.sleep,
        random_source=random_source,
        next_check_minutes=next_check_minutes,
    )


async def _request_follow_up_decision(plugin, *args):
    return await immediate_follow_up.request_follow_up_decision(plugin, *args)


async def _request_follow_up_message(plugin, *args):
    return await immediate_follow_up.request_follow_up_message(plugin, *args)


async def collect_follow_ups(
    plugin,
    session_id: str,
    session_config: dict,
    gate,
    accepted_turns: tuple[AcceptedTurn, ...],
    *,
    random_source=random.random,
) -> tuple[AcceptedTurn, ...]:
    return await immediate_follow_up.collect_follow_ups(
        plugin,
        session_id,
        session_config,
        gate,
        accepted_turns,
        dispatch=dispatch_proactive_message,
        controller=_request_follow_up_decision,
        message_controller=_request_follow_up_message,
        sleep=asyncio.sleep,
        random_source=random_source,
    )


async def _save_conversation_history(plugin, *args):
    return await proactive_history.save_conversation_history(
        plugin,
        *args,
        sleep=asyncio.sleep,
        write_pair=_write_guarded_history_pair,
    )


async def _write_guarded_history_pair(plugin, *args):
    return await proactive_history.write_guarded_history_pair(
        plugin, *args, settle_marker=_settle_history_marker_shielded
    )


def _marked_history_pair(
    user_prompt: str,
    assistant_response: str | tuple[AcceptedTurn, ...],
    marker: str,
) -> tuple[dict, dict]:
    return proactive_history.marked_history_pair(
        user_prompt, assistant_response, marker
    )


async def _settle_history_marker_shielded(
    manager: ConversationManager,
    conv_id: str,
    marker: str,
    *,
    remove_pair: bool,
) -> None:
    await proactive_history.settle_history_marker_shielded(
        manager, conv_id, marker, remove_pair=remove_pair
    )


async def _update_unanswered_and_reschedule(plugin, *args, **kwargs):
    return await proactive_state.update_unanswered_and_reschedule(
        plugin, *args, **kwargs
    )


async def _clear_regular_job_state_if_current(plugin, *args):
    return await proactive_state.clear_regular_job_state_if_current(plugin, *args)


async def _cleanup_context_task(plugin, *args):
    await proactive_state.cleanup_context_task(plugin, *args)


async def _handle_fatal_error(plugin, *args, **kwargs):
    await proactive_state.handle_fatal_error(plugin, *args, **kwargs)


async def _reschedule_quiet_source(plugin, *args):
    await proactive_state.reschedule_quiet_source(plugin, *args)


def _is_habit_job(job_id: str) -> bool:
    return proactive_state.is_habit_job(job_id)
