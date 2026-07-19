from __future__ import annotations

import json
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal


AutoCheckProfile = Literal[
    "romantic",
    "normal",
    "active",
    "very_active",
    "inactive",
    "very_inactive",
]


@dataclass(frozen=True, slots=True)
class AutoCheckProfileDefaults:
    min_interval_minutes: int
    max_interval_minutes: int
    guidance: str


@dataclass(frozen=True, slots=True)
class AutoCheckSettings:
    enable: bool = False
    profile: AutoCheckProfile = "romantic"
    use_custom_intervals: bool = False
    min_interval_minutes: int = 10
    max_interval_minutes: int = 60
    guidance: str = ""


@dataclass(frozen=True, slots=True)
class AutoCheckDecision:
    send_message: bool
    message: str
    next_check_minutes: int | None = None


PROFILE_DEFAULTS: Final[dict[AutoCheckProfile, AutoCheckProfileDefaults]] = {
    "romantic": AutoCheckProfileDefaults(
        10,
        60,
        "像熱戀中的情侶一樣親近、在意對方，偶爾自然地想起對方；不要每次都打擾，保留真實的節奏。",
    ),
    "normal": AutoCheckProfileDefaults(
        30,
        120,
        "保持自然、平衡的關心，只有在有合適話題或值得延續時才主動開口。",
    ),
    "active": AutoCheckProfileDefaults(
        10,
        45,
        "較積極地尋找延續話題的機會，但仍要避免重複、連續打擾或無內容的寒暄。",
    ),
    "very_active": AutoCheckProfileDefaults(
        3,
        20,
        "互動熱絡時可以較常主動關心和接續話題，但只有真的自然才發送，不要為了頻率硬聊。",
    ),
    "inactive": AutoCheckProfileDefaults(
        60,
        240,
        "偏安靜和克制，只有看到明確值得回應的內容或真有合適關心時才發送。",
    ),
    "very_inactive": AutoCheckProfileDefaults(
        180,
        720,
        "非常克制，長時間沒有新內容時通常不發送，避免造成壓力，只在重要且自然的時機開口。",
    ),
}

_DECISION_KEYS: Final[frozenset[str]] = frozenset(("send_message", "message"))
_DECISION_KEYS_WITH_TIMING: Final[frozenset[str]] = frozenset(
    (*_DECISION_KEYS, "next_check_minutes")
)


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
    if type(value) is not int or value <= 0:
        return None
    return max(minimum, min(value, maximum))


def resolve_auto_check_settings(session_config: object) -> AutoCheckSettings:
    if not isinstance(session_config, Mapping):
        return AutoCheckSettings()
    raw = session_config.get("auto_check_settings")
    if not isinstance(raw, Mapping):
        return AutoCheckSettings()

    profile_value = raw.get("profile", "romantic")
    profile: AutoCheckProfile = (
        profile_value
        if isinstance(profile_value, str) and profile_value in PROFILE_DEFAULTS
        else "romantic"
    )
    defaults = PROFILE_DEFAULTS[profile]
    use_custom_intervals = raw.get("use_custom_intervals", False)
    use_custom_intervals = (
        use_custom_intervals if type(use_custom_intervals) is bool else False
    )
    raw_minimum = raw.get("min_interval_minutes") if use_custom_intervals else None
    raw_maximum = raw.get("max_interval_minutes") if use_custom_intervals else None
    minimum = _bounded_int(raw_minimum, minimum=1, maximum=1440)
    maximum = _bounded_int(raw_maximum, minimum=1, maximum=2880)
    resolved_minimum = minimum if minimum is not None else defaults.min_interval_minutes
    resolved_maximum = maximum if maximum is not None else defaults.max_interval_minutes
    resolved_maximum = max(resolved_minimum, resolved_maximum)
    enable = raw.get("enable", False)
    custom_guidance = raw.get("guidance", "")
    guidance = defaults.guidance
    if isinstance(custom_guidance, str) and custom_guidance.strip():
        guidance += f"\n使用者補充指示：{custom_guidance.strip()}"
    return AutoCheckSettings(
        enable=enable if type(enable) is bool else False,
        profile=profile,
        use_custom_intervals=use_custom_intervals,
        min_interval_minutes=resolved_minimum,
        max_interval_minutes=resolved_maximum,
        guidance=guidance,
    )


def clamp_auto_check_interval(seconds: int, settings: AutoCheckSettings) -> int:
    """Keep an existing schedule interval inside the selected check range."""
    minimum = settings.min_interval_minutes * 60
    maximum = settings.max_interval_minutes * 60
    return max(minimum, min(maximum, int(seconds)))


def clamp_future_trigger_time(
    trigger_time: float,
    now: float,
    settings: AutoCheckSettings,
) -> float:
    if trigger_time <= now:
        return trigger_time
    interval = clamp_auto_check_interval(int(trigger_time - now), settings)
    return now + interval


def compute_auto_check_interval(
    schedule_settings: dict,
    settings: AutoCheckSettings,
    timezone: zoneinfo.ZoneInfo | None,
    unanswered_count: int,
) -> int:
    from .scheduler import compute_weighted_interval

    interval_mode = schedule_settings.get("interval_mode", "adaptive")
    if interval_mode == "weighted_random":
        base_interval = compute_weighted_interval(
            schedule_settings, timezone, unanswered_count
        )
    else:
        base_interval = compute_adaptive_interval(settings, unanswered_count)
    return clamp_auto_check_interval(base_interval, settings)


def compute_adaptive_interval(
    settings: AutoCheckSettings, unanswered_count: int
) -> int:
    minimum = settings.min_interval_minutes * 60
    maximum = settings.max_interval_minutes * 60
    midpoint = minimum + (maximum - minimum) // 2
    unanswered = max(0, int(unanswered_count))
    extra = min(maximum - midpoint, (maximum - minimum) * min(unanswered, 4) // 8)
    return midpoint + extra


def compute_session_interval(
    schedule_settings: dict,
    session_config: dict,
    timezone: zoneinfo.ZoneInfo | None,
    unanswered_count: int,
) -> int:
    from .scheduler import (
        compute_adaptive_interval as compute_schedule_adaptive_interval,
        compute_weighted_interval,
    )

    settings = resolve_auto_check_settings(session_config)
    if settings.enable:
        return compute_auto_check_interval(
            schedule_settings,
            settings,
            timezone,
            unanswered_count,
        )
    if schedule_settings.get("interval_mode", "adaptive") != "weighted_random":
        return compute_schedule_adaptive_interval(schedule_settings, unanswered_count)
    return compute_weighted_interval(schedule_settings, timezone, unanswered_count)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(key)
        result[key] = value
    return result


def _sanitize_message(message: str) -> str:
    return " ".join(message.split())


_MAX_AUTO_CHECK_MESSAGE_LENGTH: Final[int] = 2000


def parse_auto_check_decision(response: object) -> AutoCheckDecision | None:
    """Parse the model's strict send/no-send JSON decision."""
    if type(response) is not str:
        return None
    try:
        parsed: object = json.loads(response, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or set(parsed) not in (
        _DECISION_KEYS,
        _DECISION_KEYS_WITH_TIMING,
    ):
        return None
    send_message = parsed["send_message"]
    message = parsed["message"]
    if type(send_message) is not bool or type(message) is not str:
        return None
    next_check_minutes: int | None = None
    if "next_check_minutes" in parsed:
        value = parsed["next_check_minutes"]
        if type(value) is not int or value <= 0:
            return None
        next_check_minutes = value
    if not send_message:
        return (
            AutoCheckDecision(False, "", next_check_minutes) if message == "" else None
        )
    if len(message) > _MAX_AUTO_CHECK_MESSAGE_LENGTH:
        return None
    sanitized = _sanitize_message(message)
    return AutoCheckDecision(True, sanitized, next_check_minutes) if sanitized else None


def bounded_next_check_minutes(
    decision: AutoCheckDecision,
    settings: AutoCheckSettings,
) -> AutoCheckDecision:
    """將模型要求的下一次檢查時間限制在設定範圍內。"""
    if decision.next_check_minutes is None:
        return decision
    bounded = max(
        settings.min_interval_minutes,
        min(settings.max_interval_minutes, decision.next_check_minutes),
    )
    return AutoCheckDecision(decision.send_message, decision.message, bounded)
