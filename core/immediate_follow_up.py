from __future__ import annotations

import json
import random
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .delivery import AcceptedTurn, DispatchGate


_DECISION_KEYS = frozenset(("send_follow_up", "message"))
RandomSource = Callable[[], float]
FollowUpMode = Literal["llm", "random"]


@dataclass(frozen=True, slots=True)
class ImmediateFollowUpSettings:
    enable: bool = False
    decision_mode: FollowUpMode = "llm"
    max_follow_ups: int = 1
    debounce_seconds: int = 2
    random_probability: int = 100
    random_decay: int = 0


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
    mode_value = raw.get("decision_mode", "llm")
    decision_mode: FollowUpMode = "random" if mode_value == "random" else "llm"
    debounce_value = (
        raw["debounce_seconds"]
        if "debounce_seconds" in raw
        else raw.get("delay_seconds")
    )
    return ImmediateFollowUpSettings(
        enable=enable_value if type(enable_value) is bool else False,
        decision_mode=decision_mode,
        max_follow_ups=_bounded_int(
            raw.get("max_follow_ups"),
            default=1,
            minimum=0,
            maximum=10,
        ),
        debounce_seconds=_bounded_int(
            debounce_value,
            default=2,
            minimum=0,
            maximum=10,
        ),
        random_probability=_bounded_int(
            raw.get("random_probability"),
            default=100,
            minimum=0,
            maximum=100,
        ),
        random_decay=_bounded_int(
            raw.get("random_decay"),
            default=0,
            minimum=0,
            maximum=100,
        ),
    )


def should_send_random_follow_up(
    settings: ImmediateFollowUpSettings,
    follow_up_index: int,
    random_value: float,
) -> bool:
    probability = max(
        0,
        settings.random_probability - settings.random_decay * follow_up_index,
    )
    return random_value < probability / 100


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


async def request_follow_up_message(
    plugin,
    session_id: str,
    accepted_turns: tuple[AcceptedTurn, ...],
    gate: DispatchGate,
) -> str | None:
    from .proactive_prompt import request_follow_up_message as request

    return await request(plugin, session_id, accepted_turns, gate)


async def collect_follow_ups(
    plugin,
    session_id: str,
    session_config: dict,
    gate: DispatchGate,
    accepted_turns: tuple[AcceptedTurn, ...],
    *,
    dispatch,
    controller,
    message_controller,
    sleep,
    random_source: RandomSource = random.random,
) -> tuple[AcceptedTurn, ...]:
    from .follow_up_delivery import collect_follow_ups as collect

    return await collect(
        plugin,
        session_id,
        session_config,
        gate,
        accepted_turns,
        dispatch=dispatch,
        controller=controller,
        message_controller=message_controller,
        sleep=sleep,
        random_source=random_source,
    )


async def deliver_and_finalize(*args, **kwargs):
    from .follow_up_delivery import deliver_and_finalize as deliver

    return await deliver(*args, **kwargs)
