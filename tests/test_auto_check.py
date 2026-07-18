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


def test_auto_check_interval_clamps_existing_schedule_interval() -> None:
    module = _load_module()
    settings = module.resolve_auto_check_settings(
        {"auto_check_settings": {"enable": True, "profile": "romantic"}}
    )
    assert module.clamp_auto_check_interval(5 * 60, settings) == 10 * 60
    assert module.clamp_auto_check_interval(90 * 60, settings) == 60 * 60
    assert module.clamp_auto_check_interval(30 * 60, settings) == 30 * 60


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
