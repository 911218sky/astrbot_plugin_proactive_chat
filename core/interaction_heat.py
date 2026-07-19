from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

HeatEvent = Literal["user_activity", "proactive_delivery"]
HeatLabel = Literal["cold", "normal", "warm", "hot"]

_DEFAULT_HEAT_SCORE: Final[int] = 50
_HEAT_MIN: Final[int] = 0
_HEAT_MAX: Final[int] = 100


@dataclass(frozen=True, slots=True)
class HeatSettings:
    enable: bool = False
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


def resolve_heat_settings(session_config: object) -> HeatSettings:
    if not isinstance(session_config, Mapping):
        return HeatSettings()
    raw_value = session_config.get("immediate_follow_up_settings")
    raw = raw_value if isinstance(raw_value, Mapping) else {}
    enable = raw.get("enable", False)
    return HeatSettings(
        enable=enable if type(enable) is bool else False,
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


def apply_heat(
    score: int,
    event: HeatEvent,
    settings: HeatSettings | None = None,
) -> int:
    deltas = {
        "user_activity": settings.user_activity_delta if settings else 15,
        "proactive_delivery": settings.proactive_delivery_delta if settings else -5,
    }
    return max(_HEAT_MIN, min(_HEAT_MAX, int(score) + deltas[event]))


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
