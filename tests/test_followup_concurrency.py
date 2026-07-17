from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import anyio
import pytest

from astrbot_plugin_proactive_chat.core import chat_executor
from astrbot_plugin_proactive_chat.core.delivery import (
    DeliveryCoordinatorRegistry,
)
from astrbot_plugin_proactive_chat.tests.followup_burst_harness import (
    GatePhase,
    GateTransition,
    run_burst,
    run_gate_case,
    text_turn,
)


@dataclass(frozen=True, slots=True)
class GateCounts:
    sends: int
    controllers: int
    histories: int
    finalizations: int
    quiet_reschedules: int


def manual_gate_stop_matrix() -> str:
    cases = (
        ("before_initial", "activity"),
        ("before_initial", "quiet"),
        ("after_segment", "activity"),
        ("after_segment", "quiet"),
        ("controller", "activity"),
        ("controller", "quiet"),
    )
    counts = []
    for phase, transition in cases:
        with pytest.MonkeyPatch.context() as monkeypatch:
            counts.append(run_gate_case(monkeypatch, phase, transition)[0])
    with pytest.MonkeyPatch.context() as monkeypatch:
        cleanup_count = run_burst(monkeypatch, maximum=0).events.count("cleanup")
    encoded = tuple("".join(map(str, count)) for count in counts)
    if encoded != tuple("00000 00001 10000 10110 11010 11110".split()):
        raise RuntimeError("gate stop probe failed")
    if cleanup_count != 1:
        raise RuntimeError("gate cleanup probe failed")
    return (
        "PCF-03 PASS case=gate_stop_matrix activity_before_initial=S0C0H0F0 "
        "quiet_before_initial=S0C0H0F0Q1 activity_after_segment=S1C0H0F0 "
        "quiet_after_segment=S1C0H1F1 activity_during_controller=S1C1H0F1 "
        "quiet_during_controller=S1C1H1F1 future_sends=0 cleanup_once=1 "
        "external_sends=0"
    )


def test_baseline_merged_aliases_share_one_serial_coordinator() -> None:
    async def scenario() -> tuple[int, int, bool]:
        registry = DeliveryCoordinatorRegistry()
        alias = "old:FriendMessage:42"
        canonical = "new:FriendMessage:42"
        registry.record_activity(alias)
        registry.record_activity(alias)
        registry.record_activity(canonical)
        coordinator = registry.merge_aliases(alias, canonical)

        first_entered = anyio.Event()
        second_attempted = anyio.Event()
        second_entered = anyio.Event()
        release = anyio.Event()
        active = 0
        maximum_active = 0

        async def owner(session_id: str, first: bool) -> None:
            nonlocal active, maximum_active
            if not first:
                second_attempted.set()
            async with registry.coordinator_for(session_id).lease():
                active += 1
                maximum_active = max(maximum_active, active)
                if first:
                    first_entered.set()
                    await release.wait()
                else:
                    second_entered.set()
                active -= 1

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(owner, alias, True)
            await first_entered.wait()
            task_group.start_soon(owner, canonical, False)
            await second_attempted.wait()
            await anyio.lowlevel.checkpoint()
            overlapped_before_release = second_entered.is_set()
            release.set()

        return coordinator.revision, maximum_active, overlapped_before_release

    assert anyio.run(scenario) == (2, 1, False)


@pytest.mark.parametrize(
    ("phase", "transition", "expected"),
    (
        ("before_initial", "activity", GateCounts(0, 0, 0, 0, 0)),
        ("before_initial", "quiet", GateCounts(0, 0, 0, 0, 1)),
        ("after_segment", "activity", GateCounts(1, 0, 0, 0, 0)),
        ("after_segment", "quiet", GateCounts(1, 0, 1, 1, 0)),
        ("delay", "activity", GateCounts(1, 0, 0, 1, 0)),
        ("delay", "quiet", GateCounts(1, 0, 1, 1, 0)),
        ("controller", "activity", GateCounts(1, 1, 0, 1, 0)),
        ("controller", "quiet", GateCounts(1, 1, 1, 1, 0)),
    ),
)
def test_red_gate_stop_matrix_uses_only_event_barriers(
    monkeypatch: pytest.MonkeyPatch,
    phase: GatePhase,
    transition: GateTransition,
    expected: GateCounts,
) -> None:
    counts, _coordinators = run_gate_case(monkeypatch, phase, transition)
    actual = GateCounts(*counts)
    assert actual == expected, (
        f"PCF-03 RED: {transition} at {phase} violated the S/C/H/F/Q gate matrix"
    )


@pytest.mark.parametrize(
    ("phase", "expected"),
    (
        ("before_initial", GateCounts(0, 0, 0, 0, 0)),
        ("after_segment", GateCounts(1, 0, 0, 0, 0)),
        ("controller", GateCounts(1, 1, 0, 1, 0)),
    ),
)
def test_red_disablement_stops_every_future_effect(
    monkeypatch: pytest.MonkeyPatch, phase: GatePhase, expected: GateCounts
) -> None:
    counts, _coordinators = run_gate_case(monkeypatch, phase, "disabled")
    actual = GateCounts(*counts)
    assert actual == expected, (
        f"PCF-03 RED: disablement at {phase} must stop every future burst effect"
    )


def test_red_alias_activity_during_controller_invalidates_canonical_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts, coordinators = run_gate_case(
        monkeypatch, "controller", "activity", aliases=True
    )
    actual = GateCounts(*counts)
    assert actual == GateCounts(1, 1, 0, 1, 0) and coordinators == 1, (
        "PCF-03 RED: merged alias activity must invalidate one canonical burst"
    )


def test_red_failed_initial_cleans_source_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[int, int, int]:
        session_id = "platform:FriendMessage:42"
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(session_id)
        cleanup_calls = controller_calls = history_calls = 0

        async def preconditions(*_args, **_kwargs):
            return {"enable": True}, 0, False

        async def provider(*_args, **_kwargs):
            return "initial", "conversation", "prompt", None

        async def delivery(*_args, **_kwargs) -> bool:
            return False

        async def cleanup(*_args, **_kwargs) -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

        async def controller(*_args, **_kwargs) -> None:
            nonlocal controller_calls
            controller_calls += 1

        async def history(*_args, **_kwargs) -> None:
            nonlocal history_calls
            history_calls += 1

        patches = (
            ("_check_preconditions", preconditions),
            ("_prepare_and_call_llm", provider),
            ("_deliver_and_finalize", delivery),
            ("_cleanup_context_task", cleanup),
            ("_request_follow_up_decision", controller),
            ("_save_conversation_history", history),
        )
        for name, value in patches:
            monkeypatch.setattr(chat_executor, name, value)
        plugin = SimpleNamespace(
            _find_habit_task=lambda *_args: None,
            _delivery_coordinators=registry,
            _gate_verdict=lambda current: registry.verdict(
                current, enabled=True, quiet_hours=False
            ),
        )
        await chat_executor.check_and_chat(plugin, session_id, "context-1", gate=gate)
        return cleanup_calls, controller_calls, history_calls

    assert anyio.run(scenario) == (1, 0, 0), (
        "PCF-03 RED: failed initial dispatch must clean its source exactly once"
    )


def test_red_quiet_transition_during_history_delay_keeps_accepted_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[bool, int]:
        session_id = "platform:FriendMessage:42"
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(session_id)
        entered = anyio.Event()
        release = anyio.Event()
        quiet = False
        writes = 0

        async def delay(_seconds: float) -> None:
            entered.set()
            await release.wait()

        async def write(*_args, **_kwargs) -> bool:
            nonlocal writes
            writes += 1
            return True

        monkeypatch.setattr(chat_executor.asyncio, "sleep", delay)
        monkeypatch.setattr(chat_executor, "_write_guarded_history_pair", write)
        plugin = SimpleNamespace(
            config={},
            _history_save_lock=anyio.Lock(),
            _gate_verdict=lambda current_gate: registry.verdict(
                current_gate, enabled=True, quiet_hours=quiet
            ),
        )
        saved = False

        async def save() -> None:
            nonlocal saved
            saved = await chat_executor._save_conversation_history(
                plugin,
                {
                    "history_settings": {
                        "save_proactive_history": True,
                        "history_save_delay_seconds": 1,
                        "history_save_retry_attempts": 0,
                    }
                },
                "conversation",
                "prompt",
                (text_turn("accepted", accepted=("accepted",)),),
                gate,
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(save)
            await entered.wait()
            quiet = True
            release.set()
        return saved, writes

    assert anyio.run(scenario) == (True, 1), (
        "PCF-03 RED: quiet hours after acceptance must not erase delayed history"
    )
