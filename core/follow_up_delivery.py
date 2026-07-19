from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from typing import assert_never

from astrbot.api import logger

from .immediate_follow_up import (
    RandomSource,
    parse_follow_up_decision,
    resolve_immediate_follow_up_settings,
    should_send_random_follow_up,
)

_LOG_TAG = "[主動訊息]"
Sleep = Callable[[float], Awaitable[None]]


async def collect_follow_ups(
    plugin,
    session_id: str,
    session_config: dict,
    gate,
    accepted_turns,
    *,
    dispatch,
    controller,
    message_controller,
    sleep: Sleep,
    random_source: RandomSource,
):
    from .delivery import DispatchStatus, GateVerdict, accepted_turn_text

    settings = resolve_immediate_follow_up_settings(session_config)
    if not settings.enable:
        return accepted_turns
    turns = accepted_turns
    for index in range(settings.max_follow_ups):
        if plugin._gate_verdict(gate) is not GateVerdict.CURRENT:
            break
        delay_seconds = settings.debounce_seconds
        if delay_seconds:
            await sleep(delay_seconds)
            if plugin._gate_verdict(gate) is not GateVerdict.CURRENT:
                break
        try:
            if settings.decision_mode == "random":
                if not should_send_random_follow_up(settings, index, random_source()):
                    break
                raw = await message_controller(plugin, session_id, turns, gate)
            else:
                raw = await controller(plugin, session_id, turns, gate)
        except Exception as error:
            logger.info(f"{_LOG_TAG} 即時跟進控制器失敗，停止本輪: {error}")
            break
        if plugin._gate_verdict(gate) is not GateVerdict.CURRENT:
            break
        decision = parse_follow_up_decision(
            raw,
            accepted_turns=tuple(accepted_turn_text(turn) for turn in turns),
        )
        if decision is None or not decision.send_follow_up:
            break
        turn = await dispatch(
            session_id=session_id,
            text=decision.message,
            config=plugin.config,
            context=plugin.context,
            session_data=plugin.session_data,
            reset_group_silence_cb=plugin._reset_group_silence_timer,
            last_bot_message_time_setter=lambda value: setattr(
                plugin, "last_bot_message_time", value
            ),
            gate_check=lambda: plugin._gate_verdict(gate),
        )
        if turn.accepted_components:
            turns = (*turns, turn)
        if turn.status is not DispatchStatus.COMPLETE:
            break
    return turns


_collect_follow_ups = collect_follow_ups


async def deliver_and_finalize(
    plugin,
    session_id: str,
    session_config: dict,
    response_text: str,
    conv_id: str,
    final_prompt: str,
    unanswered_count: int,
    ctx_job_id: str,
    gate,
    *,
    dispatch,
    controller,
    message_controller,
    finalize,
    save_history,
    cleanup_context,
    clear_failed,
    reschedule_quiet,
    is_habit_job,
    sleep: Sleep,
    random_source: RandomSource = random.random,
    next_check_minutes: int | None = None,
) -> bool:
    from .delivery import DispatchStatus, GateVerdict

    initial = await dispatch(
        session_id=session_id,
        text=response_text,
        config=plugin.config,
        context=plugin.context,
        session_data=plugin.session_data,
        reset_group_silence_cb=plugin._reset_group_silence_timer,
        last_bot_message_time_setter=lambda value: setattr(
            plugin, "last_bot_message_time", value
        ),
        gate_check=lambda: plugin._gate_verdict(gate),
    )
    if initial.status is DispatchStatus.FAILED:
        if initial.verdict is GateVerdict.QUIET_HOURS:
            await reschedule_quiet(plugin, session_id, ctx_job_id)
            return True
        if (
            initial.verdict is GateVerdict.CURRENT
            and plugin._gate_verdict(gate) is GateVerdict.CURRENT
            and not is_habit_job(ctx_job_id)
        ):
            await clear_failed(plugin, session_id, gate)
        return False
    finalize_options = {
        "ctx_job_id": ctx_job_id,
        "clear_task_description": not bool(ctx_job_id),
        "gate": gate,
    }
    if next_check_minutes is not None:
        finalize_options["next_check_minutes"] = next_check_minutes
    finalized = await finalize(
        plugin,
        session_id,
        session_config,
        unanswered_count,
        **finalize_options,
    )
    if not finalized:
        return False
    turns = (initial,)
    if initial.status is DispatchStatus.COMPLETE:
        turns = await collect_follow_ups(
            plugin,
            session_id,
            session_config,
            gate,
            turns,
            dispatch=dispatch,
            controller=controller,
            message_controller=message_controller,
            sleep=sleep,
            random_source=random_source,
        )
    gate_check = getattr(plugin, "_gate_verdict", None)
    verdict = gate_check(gate) if callable(gate_check) else initial.verdict
    match verdict:
        case GateVerdict.CURRENT | GateVerdict.QUIET_HOURS:
            try:
                await save_history(
                    plugin, session_config, conv_id, final_prompt, turns, gate
                )
            except Exception as error:
                logger.info(
                    f"{_LOG_TAG} 即時跟進歷史寫回失敗，已保留已完成發送: {error}"
                )
        case GateVerdict.ACTIVITY_CHANGED | GateVerdict.DISABLED:
            pass
        case unreachable:
            assert_never(unreachable)
    if ctx_job_id:
        if is_habit_job(ctx_job_id):
            await plugin._cleanup_habit_task(session_id, ctx_job_id)
        else:
            await cleanup_context(plugin, session_id, ctx_job_id)
    return True
