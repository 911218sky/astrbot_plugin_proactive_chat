from __future__ import annotations

import json
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from astrbot.api import logger

if TYPE_CHECKING:
    from .delivery import AcceptedTurn, DispatchGate


_DECISION_KEYS = frozenset(("send_follow_up", "message"))
_LOG_TAG = "[主動訊息]"
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ImmediateFollowUpSettings:
    enable: bool = False
    max_follow_ups: int = 1
    delay_seconds: int = 2


@dataclass(frozen=True, slots=True)
class FollowUpDecision:
    send_follow_up: bool
    message: str


class _DuplicateKeyError(ValueError):
    pass


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        return default
    return max(minimum, min(value, maximum))


def resolve_immediate_follow_up_settings(
    session_config: object,
) -> ImmediateFollowUpSettings:
    if not isinstance(session_config, Mapping):
        return ImmediateFollowUpSettings()

    raw = session_config.get("immediate_follow_up_settings")
    if not isinstance(raw, Mapping):
        return ImmediateFollowUpSettings()

    enable_value = raw.get("enable", False)
    return ImmediateFollowUpSettings(
        enable=enable_value if type(enable_value) is bool else False,
        max_follow_ups=_bounded_int(
            raw.get("max_follow_ups"),
            default=1,
            minimum=0,
            maximum=3,
        ),
        delay_seconds=_bounded_int(
            raw.get("delay_seconds"),
            default=2,
            minimum=0,
            maximum=10,
        ),
    )


def sanitize_follow_up_message(message: str) -> str:
    normalized = unicodedata.normalize("NFKC", message)
    return " ".join(normalized.split())


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def parse_follow_up_decision(
    response: object,
    *,
    accepted_turns: Iterable[str],
) -> FollowUpDecision | None:
    if type(response) is not str:
        return None

    try:
        parsed: object = json.loads(
            response,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (TypeError, ValueError):
        return None

    if not isinstance(parsed, dict) or set(parsed) != _DECISION_KEYS:
        return None

    send_follow_up = parsed["send_follow_up"]
    message = parsed["message"]
    if type(send_follow_up) is not bool or type(message) is not str:
        return None

    if not send_follow_up:
        if message != "":
            return None
        return FollowUpDecision(send_follow_up=False, message="")

    sanitized = sanitize_follow_up_message(message)
    if not sanitized:
        return None

    comparison_key = sanitized.casefold()
    if any(
        comparison_key == sanitize_follow_up_message(turn).casefold()
        for turn in accepted_turns
    ):
        return None
    return FollowUpDecision(send_follow_up=True, message=sanitized)


async def request_follow_up_decision(
    plugin,
    session_id: str,
    accepted_turns: tuple[AcceptedTurn, ...],
    gate: DispatchGate,
) -> str | None:
    from .proactive_prompt import request_follow_up_decision as request

    return await request(plugin, session_id, accepted_turns, gate)


async def _collect_follow_ups(
    plugin,
    session_id: str,
    session_config: dict,
    gate: DispatchGate,
    accepted_turns: tuple[AcceptedTurn, ...],
    *,
    dispatch,
    controller,
    sleep: Sleep,
) -> tuple[AcceptedTurn, ...]:
    from .delivery import DispatchStatus, GateVerdict, accepted_turn_text

    settings = resolve_immediate_follow_up_settings(session_config)
    if not settings.enable:
        return accepted_turns
    turns = accepted_turns
    for _index in range(settings.max_follow_ups):
        if plugin._gate_verdict(gate) is not GateVerdict.CURRENT:
            break
        if settings.delay_seconds:
            await sleep(settings.delay_seconds)
            if plugin._gate_verdict(gate) is not GateVerdict.CURRENT:
                break
        try:
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


async def deliver_and_finalize(
    plugin,
    session_id: str,
    session_config: dict,
    response_text: str,
    conv_id: str,
    final_prompt: str,
    unanswered_count: int,
    ctx_job_id: str,
    gate: DispatchGate,
    *,
    dispatch,
    controller,
    finalize,
    save_history,
    cleanup_context,
    clear_failed,
    reschedule_quiet,
    is_habit_job,
    sleep: Sleep,
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
    finalized = await finalize(
        plugin,
        session_id,
        session_config,
        unanswered_count,
        ctx_job_id=ctx_job_id,
        clear_task_description=not bool(ctx_job_id),
        gate=gate,
    )
    if not finalized:
        return False
    turns = (initial,)
    if initial.status is DispatchStatus.COMPLETE:
        turns = await _collect_follow_ups(
            plugin,
            session_id,
            session_config,
            gate,
            turns,
            dispatch=dispatch,
            controller=controller,
            sleep=sleep,
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
