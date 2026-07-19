from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "core" / "auto_check.py"
    spec = importlib.util.spec_from_file_location("pcf_auto_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_schema() -> dict:
    return json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def test_schema_defines_private_auto_check_settings_and_presets() -> None:
    schema = _load_schema()
    paths = (
        ("private_settings", "items"),
        ("private_sessions", "templates", "private_session", "items"),
    )
    for path in paths:
        node = schema
        for key in path:
            node = node[key]
        block = node["auto_check_settings"]
        assert block["items"]["enable"]["default"] is False
        assert block["items"]["use_custom_intervals"]["default"] is False
        assert block["items"]["profile"]["options"] == [
            "romantic",
            "normal",
            "active",
            "very_active",
            "inactive",
            "very_inactive",
        ]
        assert block["items"]["min_interval_minutes"]["slider"] == {
            "min": 1,
            "max": 1440,
            "step": 1,
        }
        assert block["items"]["max_interval_minutes"]["slider"] == {
            "min": 1,
            "max": 2880,
            "step": 1,
        }
        schedule = node["schedule_settings"]["items"]
        assert schedule["interval_mode"]["default"] == "adaptive"
        assert schedule["interval_mode"]["options"] == [
            "adaptive",
            "weighted_random",
        ]


def test_private_schema_shows_weighted_random_controls_only_for_legacy_mode() -> None:
    schema = _load_schema()

    private_paths = (
        ("private_settings", "items", "schedule_settings", "items"),
        (
            "private_sessions",
            "templates",
            "private_session",
            "items",
            "schedule_settings",
            "items",
        ),
    )
    group_paths = (
        ("group_settings", "items", "schedule_settings", "items"),
        (
            "group_sessions",
            "templates",
            "group_session",
            "items",
            "schedule_settings",
            "items",
        ),
    )

    for path in private_paths:
        node = schema
        for key in path:
            node = node[key]
        assert node["schedule_rules"]["condition"] == {
            "interval_mode": "weighted_random"
        }
        assert node["default_decay_rate"]["condition"] == {
            "interval_mode": "weighted_random"
        }
    for path in group_paths:
        node = schema
        for key in path:
            node = node[key]
        assert "schedule_rules" in node
        assert "default_decay_rate" in node


def test_group_schema_exposes_adaptive_and_human_like_controls() -> None:
    schema = _load_schema()
    paths = (
        ("group_settings", "items"),
        ("group_sessions", "templates", "group_session", "items"),
    )
    for path in paths:
        node = schema
        for key in path:
            node = node[key]
        assert node["auto_check_settings"]["items"]["profile"]["default"] == "romantic"
        assert node["schedule_settings"]["items"]["interval_mode"]["default"] == "adaptive"
        assert node["human_like_settings"]["items"]["timing_min_seconds"]["type"] == "float"
        follow_up = node["immediate_follow_up_settings"]["items"]
        assert follow_up["initial_heat_score"]["default"] == 50
        assert follow_up["user_activity_delta"]["default"] == 15
        assert follow_up["proactive_delivery_delta"]["default"] == -5


@pytest.mark.parametrize(
    ("profile", "minimum", "maximum"),
    (
        ("romantic", 10, 60),
        ("normal", 30, 120),
        ("active", 10, 45),
        ("very_active", 3, 20),
        ("inactive", 60, 240),
        ("very_inactive", 180, 720),
    ),
)
def test_profiles_resolve_to_human_like_ranges(
    profile: str, minimum: int, maximum: int
) -> None:
    module = _load_module()
    settings = module.resolve_auto_check_settings(
        {"auto_check_settings": {"enable": True, "profile": profile}}
    )
    assert settings.enable is True
    assert settings.profile == profile
    assert (settings.min_interval_minutes, settings.max_interval_minutes) == (
        minimum,
        maximum,
    )


def test_group_auto_check_interval_uses_configured_bounds() -> None:
    module = _load_module()
    settings = module.resolve_auto_check_settings(
        {
            "auto_check_settings": {
                "enable": True,
                "use_custom_intervals": True,
                "min_interval_minutes": 12,
                "max_interval_minutes": 24,
            }
        }
    )
    interval = module.compute_adaptive_interval(settings, 0)
    assert 12 * 60 <= interval <= 24 * 60


def test_auto_check_defaults_off_and_clamps_overrides() -> None:
    module = _load_module()
    assert module.resolve_auto_check_settings({}).enable is False
    settings = module.resolve_auto_check_settings(
        {
            "auto_check_settings": {
                "enable": True,
                "profile": "unknown",
                "use_custom_intervals": True,
                "min_interval_minutes": 0,
                "max_interval_minutes": 99999,
            }
        }
    )
    assert settings.profile == "romantic"
    assert settings.min_interval_minutes == 10
    assert settings.max_interval_minutes == 2880


def test_profile_selection_overrides_romantic_schema_defaults() -> None:
    module = _load_module()
    settings = module.resolve_auto_check_settings(
        {
            "auto_check_settings": {
                "enable": True,
                "profile": "very_inactive",
                "min_interval_minutes": 10,
                "max_interval_minutes": 60,
            }
        }
    )
    assert (settings.min_interval_minutes, settings.max_interval_minutes) == (180, 720)


def test_custom_interval_switch_allows_explicit_profile_override() -> None:
    module = _load_module()
    settings = module.resolve_auto_check_settings(
        {
            "auto_check_settings": {
                "enable": True,
                "profile": "normal",
                "use_custom_intervals": True,
                "min_interval_minutes": 10,
                "max_interval_minutes": 60,
            }
        }
    )
    assert settings.use_custom_intervals is True
    assert (settings.min_interval_minutes, settings.max_interval_minutes) == (10, 60)


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ('{"send_message":false,"message":""}', (False, "")),
        ('{"send_message":true,"message":" 想你了！ "}', (True, "想你了！")),
        ('前綴 {"send_message":false,"message":""}', None),
        ('{"send_message":true,"message":""}', None),
        ('{"send_message":false,"message":"不要發"}', None),
    ),
)
def test_auto_check_decision_requires_exact_json(
    raw: str, expected: tuple[bool, str] | None
) -> None:
    module = _load_module()
    result = module.parse_auto_check_decision(raw)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert (result.send_message, result.message) == expected


@pytest.mark.parametrize(
    ("raw", "expected_minutes"),
    (
        ('{"send_message":false,"message":"","next_check_minutes":180}', 180),
        ('{"send_message":true,"message":"想你了","next_check_minutes":45}', 45),
        ('{"send_message":false,"message":""}', None),
    ),
)
def test_auto_check_decision_supports_timing_and_legacy_payloads(
    raw: str, expected_minutes: int | None
) -> None:
    module = _load_module()

    result = module.parse_auto_check_decision(raw)

    assert result is not None
    assert result.next_check_minutes == expected_minutes


@pytest.mark.parametrize(
    "raw",
    (
        '{"send_message":false,"message":"","next_check_minutes":0}',
        '{"send_message":false,"message":"","next_check_minutes":-1}',
        '{"send_message":false,"message":"","next_check_minutes":1.5}',
        '{"send_message":false,"message":"","next_check_minutes":"30"}',
        '{"send_message":false,"message":"","next_check_minutes":30,"extra":true}',
    ),
)
def test_auto_check_decision_rejects_invalid_timing_payloads(raw: str) -> None:
    module = _load_module()

    assert module.parse_auto_check_decision(raw) is None


def test_auto_check_decision_bounds_model_timing_to_profile_range() -> None:
    module = _load_module()
    settings = module.resolve_auto_check_settings(
        {"auto_check_settings": {"enable": True, "profile": "normal"}}
    )

    too_soon = module.bounded_next_check_minutes(
        module.AutoCheckDecision(False, "", 1), settings
    )
    too_late = module.bounded_next_check_minutes(
        module.AutoCheckDecision(False, "", 999), settings
    )
    legacy = module.bounded_next_check_minutes(
        module.AutoCheckDecision(False, ""), settings
    )

    assert too_soon.next_check_minutes == 30
    assert too_late.next_check_minutes == 120
    assert legacy.next_check_minutes is None


def test_auto_check_interval_clamps_existing_schedule_interval() -> None:
    module = _load_module()
    settings = module.resolve_auto_check_settings(
        {"auto_check_settings": {"enable": True, "profile": "romantic"}}
    )
    assert module.clamp_auto_check_interval(5 * 60, settings) == 10 * 60
    assert module.clamp_auto_check_interval(90 * 60, settings) == 60 * 60
    assert module.clamp_auto_check_interval(30 * 60, settings) == 30 * 60


def test_adaptive_interval_is_stable_and_respects_unanswered_count() -> None:
    settings_module = _load_module()
    settings = settings_module.resolve_auto_check_settings(
        {"auto_check_settings": {"enable": True, "profile": "normal"}}
    )

    first = settings_module.compute_adaptive_interval(settings, 0)
    repeated = settings_module.compute_adaptive_interval(settings, 0)
    after_unanswered = settings_module.compute_adaptive_interval(settings, 3)

    assert first == repeated == 75 * 60
    assert 75 * 60 < after_unanswered <= 120 * 60


def test_persisted_future_trigger_is_bounded_after_config_change() -> None:
    module = _load_module()
    settings = module.resolve_auto_check_settings(
        {"auto_check_settings": {"enable": True, "profile": "romantic"}}
    )
    assert module.clamp_future_trigger_time(90 * 60, 0, settings) == 60 * 60
    assert module.clamp_future_trigger_time(-1, 0, settings) == -1


def test_auto_check_decision_rejects_oversized_message() -> None:
    module = _load_module()
    response = json.dumps({"send_message": True, "message": "x" * 2001})
    assert module.parse_auto_check_decision(response) is None
