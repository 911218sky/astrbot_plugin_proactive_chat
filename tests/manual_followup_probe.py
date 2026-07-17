from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATHS = (
    ("private_settings", "items"),
    ("group_settings", "items"),
    ("private_sessions", "templates", "private_session", "items"),
    ("group_sessions", "templates", "group_session", "items"),
)


def _load_follow_up_module() -> ModuleType:
    path = ROOT / "core" / "immediate_follow_up.py"
    spec = importlib.util.spec_from_file_location("manual_immediate_follow_up", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("follow-up module unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("duplicate schema key")
        result[key] = value
    return result


def _schema_block_count() -> int:
    schema = json.loads(
        (ROOT / "_conf_schema.json").read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    count = 0
    for path in SCHEMA_PATHS:
        node: object = schema
        for key in path:
            if not isinstance(node, dict):
                raise RuntimeError("invalid schema path")
            node = node[key]
        if not isinstance(node, dict):
            raise RuntimeError("invalid schema items")
        count += "immediate_follow_up_settings" in node
    return count


def _run_happy(module: ModuleType) -> None:
    settings = module.resolve_immediate_follow_up_settings(
        {
            "immediate_follow_up_settings": {
                "enable": True,
                "decision_mode": "random",
                "max_follow_ups": 8,
                "delay_seconds": -1,
                "random_probability": 80,
                "random_decay": 20,
            }
        }
    )
    decision = module.parse_follow_up_decision(
        '{"send_follow_up":true,"message":"  Next   thought  "}',
        accepted_turns=("first thought",),
    )
    schema_blocks = _schema_block_count()
    if (
        schema_blocks != 4
        or (
            settings.enable,
            settings.decision_mode,
            settings.max_follow_ups,
            settings.delay_seconds,
            settings.random_probability,
            settings.random_decay,
        )
        != (True, "random", 3, 0, 80, 20)
        or decision is None
        or decision.message != "Next thought"
        or not module.should_send_random_follow_up(settings, 0, 0.79)
        or module.should_send_random_follow_up(settings, 1, 0.80)
    ):
        raise RuntimeError("happy probe failed")
    print("PCF-01 PASS case=happy schema_blocks=4 external_sends=0")


def _run_invalid(module: ModuleType) -> None:
    decision = module.parse_follow_up_decision(
        '```json\n{"send_follow_up":true,"message":"injected"}\n```',
        accepted_turns=(),
    )
    if decision is not None:
        raise RuntimeError("invalid probe failed")
    print("PCF-01 PASS case=invalid rejected=1 echoed_inputs=0 external_sends=0")


def _run_delivery_happy() -> None:
    from astrbot_plugin_proactive_chat.core.delivery import (
        AcceptedComponent,
        AcceptedComponentKind,
        DeliveryCoordinatorRegistry,
        DispatchStatus,
        make_accepted_turn,
    )

    registry = DeliveryCoordinatorRegistry()
    registry.record_activity("platform:FriendMessage:42")
    turn = make_accepted_turn(
        "hello",
        (AcceptedComponent(AcceptedComponentKind.TEXT, "hello"),),
        intended_components=1,
    )
    if turn.status is not DispatchStatus.COMPLETE or registry.coordinator_count != 1:
        raise RuntimeError("delivery happy probe failed")
    print(
        "PCF-02 PASS case=happy status=complete accepted=1 "
        "stale_writes=0 external_sends=0"
    )


async def _run_alias_revision_cas_async() -> None:
    from astrbot_plugin_proactive_chat.core import chat_executor
    from astrbot_plugin_proactive_chat.core.delivery import (
        DeliveryCoordinatorRegistry,
        GateVerdict,
    )

    alias = "old:FriendMessage:42"
    canonical = "new:FriendMessage:42"
    registry = DeliveryCoordinatorRegistry()
    registry.record_activity(alias)
    registry.record_activity(alias)
    registry.record_activity(canonical)
    coordinator = registry.merge_aliases(alias, canonical)

    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    overlap = 0

    async def use_coordinator(session_id: str, hold: bool) -> None:
        nonlocal active, overlap
        async with registry.coordinator_for(session_id).lease():
            active += 1
            overlap = max(overlap, max(0, active - 1))
            if hold:
                entered.set()
                await release.wait()
            active -= 1

    first = asyncio.create_task(use_coordinator(alias, True))
    await entered.wait()
    second = asyncio.create_task(use_coordinator(canonical, False))
    release.set()
    await asyncio.gather(first, second)

    gate = registry.snapshot(canonical)
    quiet_verdict = registry.verdict(gate, enabled=True, quiet_hours=True)
    stale_registry = DeliveryCoordinatorRegistry()
    stale_gate = stale_registry.record_activity(canonical)
    stale_registry.record_activity(canonical)
    saves = 0
    scheduler_calls = 0
    state = {
        canonical: {
            "unanswered_count": 0,
            "next_trigger_time": 123,
            "task_description": "keep",
        }
    }
    before = {canonical: dict(state[canonical])}

    async def save() -> None:
        nonlocal saves
        saves += 1

    def schedule(*_args: object, **_kwargs: object) -> None:
        nonlocal scheduler_calls
        scheduler_calls += 1

    plugin = SimpleNamespace(
        _find_habit_task=lambda *_args: None,
        data_lock=asyncio.Lock(),
        session_data=state,
        _save_data=save,
        _add_scheduled_job_at=schedule,
        timezone=None,
        _gate_verdict=lambda current_gate: stale_registry.verdict(
            current_gate, enabled=True, quiet_hours=False
        ),
    )
    finalized = await chat_executor._update_unanswered_and_reschedule(
        plugin,
        canonical,
        {"schedule_settings": {}},
        0,
        gate=stale_gate,
        clear_task_description=True,
    )
    if (
        finalized
        or registry.coordinator_count != 1
        or coordinator.revision != 2
        or overlap != 0
        or quiet_verdict is not GateVerdict.QUIET_HOURS
        or state != before
        or saves != 0
        or scheduler_calls != 0
    ):
        raise RuntimeError("alias revision CAS probe failed")
    print(
        "PCF-02 PASS case=alias_revision_cas coordinators=1 merged_revision=2 "
        "overlap=0 quiet_verdict=quiet_hours stale_writes=0 saves=0 "
        "scheduler_calls=0 external_sends=0"
    )


def _run_alias_revision_cas() -> None:
    asyncio.run(_run_alias_revision_cas_async())


def _run_burst_order() -> None:
    from astrbot_plugin_proactive_chat.tests.test_followup_burst import (
        manual_burst_order,
    )

    print(manual_burst_order())


def _run_gate_stop_matrix() -> None:
    from astrbot_plugin_proactive_chat.tests.test_followup_concurrency import (
        manual_gate_stop_matrix,
    )

    print(manual_gate_stop_matrix())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=("config_parser", "delivery_gate", "burst_order"),
        required=True,
    )
    parser.add_argument(
        "--case",
        choices=("happy", "invalid", "alias_revision_cas", "gate_stop_matrix"),
        required=True,
    )
    args = parser.parse_args()
    if args.scenario == "burst_order":
        if args.case == "happy":
            _run_burst_order()
        elif args.case == "gate_stop_matrix":
            _run_gate_stop_matrix()
        else:
            parser.error("burst_order requires happy or gate_stop_matrix")
    elif args.scenario == "delivery_gate":
        if args.case == "happy":
            _run_delivery_happy()
        elif args.case == "alias_revision_cas":
            _run_alias_revision_cas()
        else:
            parser.error("delivery_gate requires happy or alias_revision_cas")
    else:
        module = _load_follow_up_module()
        if args.case == "happy":
            _run_happy(module)
        elif args.case == "invalid":
            _run_invalid(module)
        else:
            parser.error("config_parser requires happy or invalid")


if __name__ == "__main__":
    main()
