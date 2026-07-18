from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Final, Literal


HeatEvent = Literal["user_activity", "proactive_delivery"]
HeatLabel = Literal["cold", "normal", "warm", "hot"]

_DEFAULT_TIMING_MIN_SECONDS: Final[int] = 2
_DEFAULT_TIMING_MAX_SECONDS: Final[int] = 8
_MAX_TIMING_SECONDS: Final[int] = 60
_DEFAULT_HEAT_SCORE: Final[int] = 50
_HEAT_MIN: Final[int] = 0
_HEAT_MAX: Final[int] = 100
_DELIVERY_WINDOW_SECONDS: Final[int] = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class HumanLikeSettings:
    enable: bool = False
    timing_min_seconds: int = _DEFAULT_TIMING_MIN_SECONDS
    timing_max_seconds: int = _DEFAULT_TIMING_MAX_SECONDS
    long_message_chars: int = 120
    long_message_bonus_seconds: int = 2
    night_bonus_seconds: int = 2
    cooldown_after_unanswered: int = 0
    cooldown_minutes: int = 120
    max_proactive_per_hour: int = 0
    max_proactive_per_day: int = 0
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


def resolve_human_like_settings(session_config: object) -> HumanLikeSettings:
    if not isinstance(session_config, Mapping):
        return HumanLikeSettings()
    raw = session_config.get("human_like_settings")
    if not isinstance(raw, Mapping):
        return HumanLikeSettings()
    minimum = _bounded_int(
        raw.get("timing_min_seconds"),
        default=_DEFAULT_TIMING_MIN_SECONDS,
        minimum=0,
        maximum=_MAX_TIMING_SECONDS,
    )
    maximum = _bounded_int(
        raw.get("timing_max_seconds"),
        default=_DEFAULT_TIMING_MAX_SECONDS,
        minimum=0,
        maximum=_MAX_TIMING_SECONDS,
    )
    if minimum > maximum:
        minimum, maximum = _DEFAULT_TIMING_MIN_SECONDS, _DEFAULT_TIMING_MAX_SECONDS
    enable = raw.get("enable", False)
    return HumanLikeSettings(
        enable=enable if type(enable) is bool else False,
        timing_min_seconds=minimum,
        timing_max_seconds=maximum,
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
        cooldown_after_unanswered=_bounded_int(
            raw.get("cooldown_after_unanswered"),
            default=0,
            minimum=0,
            maximum=10,
        ),
        cooldown_minutes=_bounded_int(
            raw.get("cooldown_minutes"),
            default=120,
            minimum=0,
            maximum=1440,
        ),
        max_proactive_per_hour=_bounded_int(
            raw.get("max_proactive_per_hour"),
            default=0,
            minimum=0,
            maximum=20,
        ),
        max_proactive_per_day=_bounded_int(
            raw.get("max_proactive_per_day"),
            default=0,
            minimum=0,
            maximum=100,
        ),
        initial_heat_score=_bounded_int(
            raw.get("initial_heat_score"),
            default=_DEFAULT_HEAT_SCORE,
            minimum=_HEAT_MIN,
            maximum=_HEAT_MAX,
        ),
        user_activity_delta=_bounded_int(
            raw.get("user_activity_delta"),
            default=15,
            minimum=-100,
            maximum=100,
        ),
        proactive_delivery_delta=_bounded_int(
            raw.get("proactive_delivery_delta"),
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
) -> int:
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
    return minimum + int((maximum - minimum) * bounded_random)


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


def normalize_cooldown_until(value: object) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return 0.0
    return timestamp if timestamp > 0 else 0.0


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


def should_enter_cooldown(
    unanswered_count: int,
    settings: HumanLikeSettings,
) -> bool:
    return (
        settings.enable
        and settings.cooldown_after_unanswered > 0
        and unanswered_count >= settings.cooldown_after_unanswered
    )


def cooldown_is_active(cooldown_until: float, now: float) -> bool:
    return cooldown_until > now


def is_outreach_capped(
    settings: HumanLikeSettings,
    sent_this_hour: int,
    sent_today: int,
    unanswered_count: int,
) -> bool:
    if not settings.enable:
        return False
    if should_enter_cooldown(unanswered_count, settings):
        return True
    return (
        settings.max_proactive_per_hour > 0
        and sent_this_hour >= settings.max_proactive_per_hour
    ) or (
        settings.max_proactive_per_day > 0
        and sent_today >= settings.max_proactive_per_day
    )


def normalize_delivery_timestamps(values: object, now: float) -> list[float]:
    if not isinstance(values, list):
        return []
    cutoff = now - _DELIVERY_WINDOW_SECONDS
    timestamps: list[float] = []
    for value in values:
        if type(value) not in (int, float):
            continue
        timestamp = float(value)
        if cutoff <= timestamp <= now:
            timestamps.append(timestamp)
    return timestamps


def delivery_counts(
    values: object,
    now: float,
    timezone: tzinfo | None = None,
) -> tuple[int, int, list[float]]:
    timestamps = normalize_delivery_timestamps(values, now)
    hour_cutoff = now - 60 * 60
    today = datetime.fromtimestamp(now, tz=timezone).date()
    hourly = sum(timestamp >= hour_cutoff for timestamp in timestamps)
    daily = sum(
        datetime.fromtimestamp(timestamp, tz=timezone).date() == today
        for timestamp in timestamps
    )
    return hourly, daily, timestamps
