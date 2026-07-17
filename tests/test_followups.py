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
    for block in blocks:
        assert isinstance(block, dict)
        assert block["type"] == "object"
        items = block["items"]
        assert isinstance(items, dict)
        assert set(items) == {"enable", "max_follow_ups", "delay_seconds"}
        assert items["enable"]["type"] == "bool"
        assert items["enable"]["default"] is False
        assert items["max_follow_ups"]["type"] == "int"
        assert items["max_follow_ups"]["default"] == 1
        assert items["max_follow_ups"]["slider"] == {
            "min": 0,
            "max": 3,
            "step": 1,
        }
        assert items["delay_seconds"]["type"] == "int"
        assert items["delay_seconds"]["default"] == 2
        assert items["delay_seconds"]["slider"] == {
            "min": 0,
            "max": 10,
            "step": 1,
        }


@pytest.mark.parametrize(
    ("session_config", "expected"),
    (
        ({}, (False, 1, 2)),
        ({"enable": True, "max_follow_ups": 3}, (False, 1, 2)),
        ({"immediate_follow_up_settings": None}, (False, 1, 2)),
        (
            {
                "immediate_follow_up_settings": {
                    "enable": True,
                    "max_follow_ups": -7,
                    "delay_seconds": 99,
                }
            },
            (True, 0, 10),
        ),
        (
            {
                "immediate_follow_up_settings": {
                    "enable": True,
                    "max_follow_ups": 99,
                    "delay_seconds": -4,
                }
            },
            (True, 3, 0),
        ),
        (
            {
                "immediate_follow_up_settings": {
                    "enable": "true",
                    "max_follow_ups": True,
                    "delay_seconds": "5",
                }
            },
            (False, 1, 2),
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
        settings.max_follow_ups,
        settings.delay_seconds,
    ) == expected


def test_settings_value_object_is_frozen_and_slotted() -> None:
    module = _load_follow_up_module()
    settings = module.resolve_immediate_follow_up_settings({})

    assert not hasattr(settings, "__dict__")
    with pytest.raises(FrozenInstanceError):
        settings.enable = True


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
