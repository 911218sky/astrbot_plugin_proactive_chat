from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal


HeatEvent = Literal["user_activity", "proactive_delivery"]
HeatLabel = Literal["cold", "normal", "warm", "hot"]

_DEFAULT_TIMING_MIN_SECONDS: Final[float] = 1.0
_DEFAULT_TIMING_MAX_SECONDS: Final[float] = 3.0
_MAX_TIMING_SECONDS: Final[float] = 60.0
_DEFAULT_INBOUND_DEBOUNCE_SECONDS: Final[int] = 3
_MAX_INBOUND_DEBOUNCE_SECONDS: Final[int] = 30
_DEFAULT_HEAT_SCORE: Final[int] = 50
_HEAT_MIN: Final[int] = 0
_HEAT_MAX: Final[int] = 100


@dataclass(frozen=True, slots=True)
class HumanLikeSettings:
    enable: bool = False
    timing_min_seconds: float = _DEFAULT_TIMING_MIN_SECONDS
    timing_max_seconds: float = _DEFAULT_TIMING_MAX_SECONDS
    inbound_debounce_seconds: int = _DEFAULT_INBOUND_DEBOUNCE_SECONDS
    long_message_chars: int = 120
    long_message_bonus_seconds: int = 2
    night_bonus_seconds: int = 2
    initial_heat_score: int = _DEFAULT_HEAT_SCORE
    user_activity_delta: int = 15
    proactive_delivery_delta: int = -5


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int:
        return default
    return max(minimum, min(value, maximum))


def _bounded_float(
    value: object,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if type(value) not in (int, float):
        return default
    return max(minimum, min(float(value), maximum))


def resolve_human_like_settings(session_config: object) -> HumanLikeSettings:
    if not isinstance(session_config, Mapping):
        return HumanLikeSettings()
    raw_value = session_config.get("human_like_settings")
    raw = raw_value if isinstance(raw_value, Mapping) else {}
    follow_up_value = session_config.get("immediate_follow_up_settings")
    follow_up = follow_up_value if isinstance(follow_up_value, Mapping) else {}
    heat_source = (
        follow_up
        if any(
            key in follow_up
            for key in (
                "initial_heat_score",
                "user_activity_delta",
                "proactive_delivery_delta",
            )
        )
        else raw
    )
    minimum = _bounded_float(
        raw.get("timing_min_seconds"),
        default=_DEFAULT_TIMING_MIN_SECONDS,
        minimum=0.0,
        maximum=_MAX_TIMING_SECONDS,
    )
    maximum = _bounded_float(
        raw.get("timing_max_seconds"),
        default=_DEFAULT_TIMING_MAX_SECONDS,
        minimum=0.0,
        maximum=_MAX_TIMING_SECONDS,
    )
    if minimum > maximum:
        minimum, maximum = _DEFAULT_TIMING_MIN_SECONDS, _DEFAULT_TIMING_MAX_SECONDS
    enable = raw.get("enable", False)
    return HumanLikeSettings(
        enable=enable if type(enable) is bool else False,
        timing_min_seconds=minimum,
        timing_max_seconds=maximum,
        inbound_debounce_seconds=_bounded_int(
            raw.get("inbound_debounce_seconds"),
            default=_DEFAULT_INBOUND_DEBOUNCE_SECONDS,
            minimum=0,
            maximum=_MAX_INBOUND_DEBOUNCE_SECONDS,
        ),
        long_message_chars=_bounded_int(
            raw.get("long_message_chars"),
            default=120,
            minimum=1,
            maximum=2000,
        ),
        long_message_bonus_seconds=_bounded_int(
            raw.get("long_message_bonus_seconds"),
            default=2,
            minimum=0,
            maximum=30,
        ),
        night_bonus_seconds=_bounded_int(
            raw.get("night_bonus_seconds"),
            default=2,
            minimum=0,
            maximum=30,
        ),
        initial_heat_score=_bounded_int(
            heat_source.get("initial_heat_score"),
            default=_DEFAULT_HEAT_SCORE,
            minimum=_HEAT_MIN,
            maximum=_HEAT_MAX,
        ),
        user_activity_delta=_bounded_int(
            heat_source.get("user_activity_delta"),
            default=15,
            minimum=-100,
            maximum=100,
        ),
        proactive_delivery_delta=_bounded_int(
            heat_source.get("proactive_delivery_delta"),
            default=-5,
            minimum=-100,
            maximum=100,
        ),
    )


def compute_follow_up_delay_seconds(
    message: str,
    local_hour: int,
    settings: HumanLikeSettings,
    random_value: float,
) -> float:
    if not settings.enable:
        return settings.timing_min_seconds
    bonus = 0
    if len(message) >= settings.long_message_chars:
        bonus += settings.long_message_bonus_seconds
    if local_hour >= 23 or local_hour < 7:
        bonus += settings.night_bonus_seconds
    minimum = settings.timing_min_seconds + bonus
    maximum = settings.timing_max_seconds + bonus
    bounded_random = max(0.0, min(1.0, float(random_value)))
    return minimum + (maximum - minimum) * bounded_random


def apply_heat(
    score: int,
    event: HeatEvent,
    settings: HumanLikeSettings | None = None,
) -> int:
    deltas = {
        "user_activity": settings.user_activity_delta if settings else 15,
        "proactive_delivery": settings.proactive_delivery_delta if settings else -5,
    }
    delta = deltas[event]
    return max(_HEAT_MIN, min(_HEAT_MAX, int(score) + delta))


def normalize_heat_score(value: object, default: int = _DEFAULT_HEAT_SCORE) -> int:
    if type(value) is not int:
        return max(_HEAT_MIN, min(_HEAT_MAX, int(default)))
    return max(_HEAT_MIN, min(_HEAT_MAX, value))


def heat_label(score: int) -> HeatLabel:
    bounded_score = max(_HEAT_MIN, min(_HEAT_MAX, int(score)))
    if bounded_score < 30:
        return "cold"
    if bounded_score < 60:
        return "normal"
    if bounded_score < 80:
        return "warm"
    return "hot"


def heat_guidance(label: HeatLabel) -> str:
    return {
        "cold": "互動偏冷，保持克制，避免連續打擾。",
        "normal": "互動正常，維持自然且平衡的節奏。",
        "warm": "互動偏熱，可以更自然地延續話題，但不要硬聊。",
        "hot": "互動熱絡，可以親近地接話，但仍要尊重對方節奏。",
    }[label]
