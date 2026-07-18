from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "core" / "human_like.py"
    spec = importlib.util.spec_from_file_location("pcf_human_like", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_defaults_keep_human_like_disabled() -> None:
    module = _load_module()
    settings = module.resolve_human_like_settings({})
    assert settings.enable is False
    assert settings.inbound_debounce_seconds == 3
    assert module.compute_follow_up_delay_seconds("hello", 12, settings, 0.5) == 2


def test_follow_up_delay_uses_length_and_night_buckets() -> None:
    module = _load_module()
    settings = module.resolve_human_like_settings(
        {
            "human_like_settings": {
                "enable": True,
                "timing_min_seconds": 1,
                "timing_max_seconds": 5,
                "long_message_chars": 10,
                "long_message_bonus_seconds": 2,
                "night_bonus_seconds": 3,
            }
        }
    )
    assert module.compute_follow_up_delay_seconds("short", 12, settings, 0.0) == 1
    assert module.compute_follow_up_delay_seconds("short", 23, settings, 0.0) == 4
    assert module.compute_follow_up_delay_seconds("long message", 12, settings, 0.99) == 6
    assert module.compute_follow_up_delay_seconds("long message", 23, settings, 0.99) == 9


def test_heat_transitions_are_clamped_and_labeled() -> None:
    module = _load_module()
    assert module.apply_heat(50, "user_activity") == 65
    assert module.apply_heat(50, "proactive_delivery") == 45
    assert module.apply_heat(0, "proactive_delivery") == 0
    assert module.apply_heat(100, "user_activity") == 100
    assert module.heat_label(10) == "cold"
    assert module.heat_label(50) == "normal"
    assert module.heat_label(70) == "warm"
    assert module.heat_label(90) == "hot"


def test_heat_deltas_are_configurable_and_still_clamped() -> None:
    module = _load_module()
    settings = module.resolve_human_like_settings(
        {
            "immediate_follow_up_settings": {
                "enable": True,
                "initial_heat_score": 20,
                "user_activity_delta": 30,
                "proactive_delivery_delta": -12,
            }
        }
    )

    assert settings.initial_heat_score == 20
    assert settings.user_activity_delta == 30
    assert settings.proactive_delivery_delta == -12
    assert module.apply_heat(20, "user_activity", settings) == 50
    assert module.apply_heat(20, "proactive_delivery", settings) == 8
    assert module.apply_heat(95, "user_activity", settings) == 100
    assert module.apply_heat(5, "proactive_delivery", settings) == 0


def test_legacy_heat_settings_remain_a_fallback() -> None:
    module = _load_module()
    settings = module.resolve_human_like_settings(
        {
            "human_like_settings": {
                "enable": True,
                "initial_heat_score": 20,
                "user_activity_delta": 30,
                "proactive_delivery_delta": -12,
            }
        }
    )

    assert settings.initial_heat_score == 20
    assert settings.user_activity_delta == 30
    assert settings.proactive_delivery_delta == -12


def test_cooldown_and_caps_are_explicit() -> None:
    module = _load_module()
    settings = module.resolve_human_like_settings(
        {
            "human_like_settings": {
                "enable": True,
                "cooldown_after_unanswered": 3,
                "cooldown_minutes": 30,
                "max_proactive_per_hour": 2,
                "max_proactive_per_day": 4,
            }
        }
    )
    assert module.should_enter_cooldown(2, settings) is False
    assert module.should_enter_cooldown(3, settings) is True
    assert module.cooldown_is_active(1200, 1100) is True
    assert module.cooldown_is_active(1000, 1000) is False
    assert module.is_outreach_capped(settings, 1, 2, 3) is True
    assert module.is_outreach_capped(settings, 1, 1, 3) is True
    assert module.is_outreach_capped(settings, 1, 1, 2) is False


def test_invalid_values_are_clamped_without_enabling_feature() -> None:
    module = _load_module()
    settings = module.resolve_human_like_settings(
        {
            "human_like_settings": {
                "enable": "true",
                "timing_min_seconds": 100,
                "timing_max_seconds": -1,
                "cooldown_minutes": 99999,
            }
        }
    )
    assert settings.enable is False
    assert settings.timing_min_seconds == 2
    assert settings.timing_max_seconds == 8
    assert settings.cooldown_minutes == 1440


def test_inbound_debounce_is_clamped_to_a_short_quiet_window() -> None:
    module = _load_module()
    settings = module.resolve_human_like_settings(
        {
            "human_like_settings": {
                "enable": True,
                "inbound_debounce_seconds": 99,
            }
        }
    )
    assert settings.inbound_debounce_seconds == 30


def test_delivery_counts_prune_old_and_invalid_values() -> None:
    module = _load_module()
    hourly, daily, timestamps = module.delivery_counts(
        [350, 3_601, 3_900, "bad", 9_999],
        4_000,
    )
    assert hourly == 2
    assert daily == 3
    assert timestamps == [350.0, 3_601.0, 3_900.0]


def test_corrupt_persisted_values_use_safe_defaults() -> None:
    module = _load_module()
    assert module.normalize_heat_score("broken") == 50
    assert module.normalize_heat_score(120) == 100
    assert module.normalize_cooldown_until("broken") == 0
