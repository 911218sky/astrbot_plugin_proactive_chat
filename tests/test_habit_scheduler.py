from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    path = ROOT / "core" / "scheduler.py"
    spec = importlib.util.spec_from_file_location("pcf_scheduler", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_adaptive_habit_window_uses_one_stable_opportunity() -> None:
    module = _load_module()
    now = datetime(2026, 7, 18, 9, 0)
    config = {
        "enable": True,
        "adaptive_timing": True,
        "habit_rules": [
            {
                "name": "早安",
                "start_hour": 8,
                "start_minute": 0,
                "end_hour": 10,
                "end_minute": 0,
                "appear_chance": 0,
            }
        ],
    }

    first = module.compute_habit_next_run(config, now=now)
    repeated = module.compute_habit_next_run(config, now=now)

    assert first[0] == repeated[0]
    assert first[0] is not None
    assert first[0].hour == 9
    assert first[0].minute == 30
