from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATHS = (
    ("private_settings", "items"),
    ("group_settings", "items"),
    ("private_sessions", "templates", "private_session", "items"),
    ("group_sessions", "templates", "group_session", "items"),
)
PRIVATE_SCHEMA_PATHS = SCHEMA_PATHS[0:1] + SCHEMA_PATHS[2:3]


def _load_source_module(name: str, path: Path) -> ModuleType:
    assert path.is_file(), f"PCF-01 RED: missing implementation {path.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _schema_items(
    schema: dict[str, object], path: tuple[str, ...]
) -> dict[str, object]:
    node: object = schema
    for key in path:
        assert isinstance(node, dict)
        node = node[key]
    assert isinstance(node, dict)
    return node


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(key)
        result[key] = value
    return result


def _load_schema() -> dict[str, object]:
    schema = json.loads(
        (ROOT / "_conf_schema.json").read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    assert isinstance(schema, dict)
    return schema


def _load_follow_up_module() -> ModuleType:
    return _load_source_module(
        "pcf01_immediate_follow_up", ROOT / "core" / "immediate_follow_up.py"
    )


def test_baseline_schema_follow_up_blocks_are_atomic() -> None:
    schema = _load_schema()
    present = sum(
        "immediate_follow_up_settings" in _schema_items(schema, path)
        for path in SCHEMA_PATHS
    )

    assert present in (0, len(SCHEMA_PATHS))


@pytest.mark.parametrize(
    "raw",
    (
        '```json\n{"send_follow_up": false, "message": ""}\n```',
        'prefix {"send_follow_up": false, "message": ""} suffix',
    ),
)
def test_baseline_permissive_parser_accepts_non_whole_json(raw: str) -> None:
    utils = _load_source_module("pcf01_baseline_utils", ROOT / "core" / "utils.py")

    assert utils.parse_llm_json(raw, expect_type=dict) == {
        "send_follow_up": False,
        "message": "",
    }


def test_schema_defines_exact_follow_up_defaults_and_bounds() -> None:
    schema = _load_schema()
    blocks = [
        _schema_items(schema, path).get("immediate_follow_up_settings")
        for path in SCHEMA_PATHS
    ]

    assert all(isinstance(block, dict) for block in blocks), (
        "PCF-01 RED: all four schema blocks must exist"
    )
    for path, block in zip(SCHEMA_PATHS, blocks, strict=True):
        assert isinstance(block, dict)
        assert block["type"] == "object"
        items = block["items"]
        assert isinstance(items, dict)
        expected_keys = {
            "enable",
            "decision_mode",
            "max_follow_ups",
            "delay_seconds",
            "random_probability",
            "random_decay",
        }
        if path in PRIVATE_SCHEMA_PATHS:
            expected_keys |= {
                "initial_heat_score",
                "user_activity_delta",
                "proactive_delivery_delta",
            }
        assert set(items) == expected_keys
        assert items["enable"]["type"] == "bool"
        assert items["enable"]["default"] is False
        assert items["decision_mode"]["type"] == "string"
        assert items["decision_mode"]["options"] == ["llm", "random"]
        assert items["decision_mode"]["default"] == "llm"
        assert items["max_follow_ups"]["type"] == "int"
        assert items["max_follow_ups"]["default"] == 1
        assert items["max_follow_ups"]["slider"] == {
            "min": 0,
            "max": 10,
            "step": 1,
        }
        assert items["delay_seconds"]["type"] == "int"
        assert items["delay_seconds"]["default"] == 2
        assert items["delay_seconds"]["slider"] == {
            "min": 0,
            "max": 10,
            "step": 1,
        }
        assert items["random_probability"]["type"] == "int"
        assert items["random_probability"]["default"] == 100
        assert items["random_probability"]["condition"] == {"decision_mode": "random"}
        assert items["random_probability"]["slider"] == {
            "min": 0,
            "max": 100,
            "step": 1,
        }
        assert items["random_decay"]["type"] == "int"
        assert items["random_decay"]["default"] == 0
        assert items["random_decay"]["condition"] == {"decision_mode": "random"}
        assert items["random_decay"]["slider"] == {
            "min": 0,
            "max": 100,
            "step": 1,
        }
        if path in PRIVATE_SCHEMA_PATHS:
            assert items["initial_heat_score"]["default"] == 50
            assert items["initial_heat_score"]["slider"] == {
                "min": 0,
                "max": 100,
                "step": 1,
            }
            assert items["user_activity_delta"]["default"] == 15
            assert items["user_activity_delta"]["slider"] == {
                "min": -100,
                "max": 100,
                "step": 1,
            }
            assert items["proactive_delivery_delta"]["default"] == -5
            assert items["proactive_delivery_delta"]["slider"] == {
                "min": -100,
                "max": 100,
                "step": 1,
            }


@pytest.mark.parametrize(
    ("session_config", "expected"),
    (
        ({}, (False, "llm", 1, 2, 100, 0)),
        ({"enable": True, "max_follow_ups": 3}, (False, "llm", 1, 2, 100, 0)),
        ({"immediate_follow_up_settings": None}, (False, "llm", 1, 2, 100, 0)),
        (
            {
                "immediate_follow_up_settings": {
                    "enable": True,
                    "decision_mode": "random",
                    "max_follow_ups": -7,
                    "delay_seconds": 99,
                    "random_probability": -1,
                    "random_decay": 101,
                }
            },
            (True, "random", 0, 10, 0, 100),
        ),
        (
            {
                "immediate_follow_up_settings": {
                    "enable": True,
                    "decision_mode": "random",
                    "max_follow_ups": 99,
                    "delay_seconds": -4,
                    "random_probability": 101,
                    "random_decay": -2,
                }
            },
            (True, "random", 10, 0, 100, 0),
        ),
        (
            {
                "immediate_follow_up_settings": {
                    "enable": "true",
                    "decision_mode": "other",
                    "max_follow_ups": True,
                    "delay_seconds": "5",
                    "random_probability": True,
                    "random_decay": "5",
                }
            },
            (False, "llm", 1, 2, 100, 0),
        ),
    ),
)
def test_settings_default_off_and_clamp_exact_integers(
    session_config: object,
    expected: tuple[bool, int, int],
) -> None:
    module = _load_follow_up_module()

    settings = module.resolve_immediate_follow_up_settings(session_config)

    assert (
        settings.enable,
        settings.decision_mode,
        settings.max_follow_ups,
        settings.debounce_seconds,
        settings.random_probability,
        settings.random_decay,
    ) == expected


def test_settings_value_object_is_frozen_and_slotted() -> None:
    module = _load_follow_up_module()
    settings = module.resolve_immediate_follow_up_settings({})

    assert not hasattr(settings, "__dict__")
    with pytest.raises(FrozenInstanceError):
        settings.enable = True


def test_settings_prefers_debounce_and_reads_legacy_delay() -> None:
    module = _load_follow_up_module()

    legacy = module.resolve_immediate_follow_up_settings(
        {"immediate_follow_up_settings": {"delay_seconds": 4}}
    )
    current = module.resolve_immediate_follow_up_settings(
        {
            "immediate_follow_up_settings": {
                "delay_seconds": 4,
                "debounce_seconds": 1,
            }
        }
    )

    assert legacy.debounce_seconds == 4
    assert current.debounce_seconds == 1


@pytest.mark.parametrize(
    ("probability", "decay", "index", "random_value", "expected"),
    (
        (100, 0, 0, 0.99, True),
        (0, 0, 0, 0.0, False),
        (80, 20, 0, 0.79, True),
        (80, 20, 1, 0.80, False),
        (80, 20, 2, 0.60, False),
    ),
)
def test_random_strategy_uses_probability_and_linear_decay(
    probability: int,
    decay: int,
    index: int,
    random_value: float,
    expected: bool,
) -> None:
    module = _load_follow_up_module()
    settings = module.ImmediateFollowUpSettings(
        enable=True,
        decision_mode="random",
        max_follow_ups=3,
        debounce_seconds=0,
        random_probability=probability,
        random_decay=decay,
    )

    assert (
        module.should_send_random_follow_up(settings, index, random_value) is expected
    )


@pytest.mark.parametrize(
    ("raw", "send_follow_up", "message"),
    (
        ('{"send_follow_up":false,"message":""}', False, ""),
        (
            '{"send_follow_up":true,"message":"  Ｈｅｌｌｏ\\n  WORLD  "}',
            True,
            "Hello WORLD",
        ),
    ),
)
def test_parser_accepts_only_valid_decisions(
    raw: str,
    send_follow_up: bool,
    message: str,
) -> None:
    module = _load_follow_up_module()

    decision = module.parse_follow_up_decision(raw, accepted_turns=())

    assert decision is not None
    assert decision.send_follow_up is send_follow_up
    assert decision.message == message


@pytest.mark.parametrize(
    "raw",
    (
        "",
        '```json\n{"send_follow_up":false,"message":""}\n```',
        'prefix {"send_follow_up":false,"message":""}',
        '{"send_follow_up":false,"message":""} trailing',
        '[{"send_follow_up":false,"message":""}]',
        "null",
        '{"send_follow_up":false}',
        '{"send_follow_up":false,"message":"","extra":0}',
        '{"send_follow_up":false,"send_follow_up":true,"message":"next"}',
        '{"send_follow_up":1,"message":"next"}',
        '{"send_follow_up":"true","message":"next"}',
        '{"send_follow_up":true,"message":7}',
        '{"send_follow_up":false,"message":"not empty"}',
        '{"send_follow_up":false,"message":"  "}',
        '{"send_follow_up":true,"message":" \\n \\t "}',
    ),
)
def test_parser_rejects_malformed_or_coerced_decisions(raw: str) -> None:
    module = _load_follow_up_module()

    assert module.parse_follow_up_decision(raw, accepted_turns=()) is None


@pytest.mark.parametrize(
    ("candidate", "accepted"),
    (
        (" STRASSE ", ("Straße",)),
        ("Ｆｏｌｌｏｗ   UP", ("follow up",)),
        ("same\n\tmessage", ("  SAME message  ", "another turn")),
    ),
)
def test_parser_rejects_normalized_duplicate_of_any_accepted_turn(
    candidate: str,
    accepted: tuple[str, ...],
) -> None:
    module = _load_follow_up_module()
    raw = json.dumps({"send_follow_up": True, "message": candidate})

    assert module.parse_follow_up_decision(raw, accepted_turns=accepted) is None


def test_parser_keeps_distinct_sanitized_message() -> None:
    module = _load_follow_up_module()
    raw = json.dumps({"send_follow_up": True, "message": "  Fresh   thought  "})

    decision = module.parse_follow_up_decision(
        raw,
        accepted_turns=("earlier thought", "another turn"),
    )

    assert decision is not None
    assert decision.message == "Fresh thought"
