from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest

from astrbot_plugin_proactive_chat.core import chat_executor
from astrbot_plugin_proactive_chat.core.delivery import (
    AcceptedComponent,
    AcceptedComponentKind,
    AcceptedTurn,
    DeliveryCoordinatorRegistry,
    DispatchStatus,
    make_accepted_turn,
)
from astrbot_plugin_proactive_chat.tests.followup_burst_harness import (
    run_burst,
    text_turn,
)


def manual_burst_order() -> str:
    with pytest.MonkeyPatch.context() as monkeypatch:
        result = run_burst(
            monkeypatch,
            decisions=('{"send_follow_up":true,"message":"second"}',),
            follow_up_turns=(text_turn("second", accepted=("second",)),),
        )
    if result.events != ["initial", "finalize", "followup", "history", "cleanup"]:
        raise RuntimeError("burst order probe failed")
    if len(result.history_payloads) != 1:
        raise RuntimeError("burst history probe failed")
    return (
        "PCF-03 PASS case=happy order=initial,finalize,followup,history,cleanup "
        "history_writes=1 external_sends=0"
    )


def test_baseline_complete_initial_turn_finalizes_before_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[bool, list[str]]:
        session_id = "platform:FriendMessage:42"
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(session_id)
        events: list[str] = []

        async def dispatch(**_kwargs):
            events.append("initial")
            component = AcceptedComponent(AcceptedComponentKind.TEXT, "initial")
            return make_accepted_turn("initial", (component,), intended_components=1)

        async def finalize(*_args, **_kwargs) -> bool:
            events.append("finalize")
            return True

        async def save_history(*_args, **_kwargs) -> bool:
            events.append("history")
            return True

        monkeypatch.setattr(chat_executor, "dispatch_proactive_message", dispatch)
        monkeypatch.setattr(
            chat_executor, "_update_unanswered_and_reschedule", finalize
        )
        monkeypatch.setattr(chat_executor, "_save_conversation_history", save_history)
        plugin = SimpleNamespace(
            config={},
            context=SimpleNamespace(),
            session_data={},
            last_bot_message_time=0.0,
            _reset_group_silence_timer=lambda *_args: None,
        )
        completed = await chat_executor._deliver_and_finalize(
            plugin,
            session_id,
            {},
            "initial",
            "conversation",
            "prompt",
            0,
            "",
            gate,
        )
        return completed, events

    assert anyio.run(scenario) == (True, ["initial", "finalize", "history"])


def test_red_happy_burst_has_exact_single_owner_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_burst(
        monkeypatch,
        decisions=('{"send_follow_up":true,"message":"second"}',),
        follow_up_turns=(text_turn("second", accepted=("second",)),),
    )
    assert result.completed, (
        "PCF-03 RED: happy burst must complete without surfacing controller/history errors"
    )
    assert result.events == [
        "initial",
        "finalize",
        "followup",
        "history",
        "cleanup",
    ], "PCF-03 RED: burst order must be initial,finalize,followup,history,cleanup"
    assert result.sent_messages == ["initial", "second"]


def test_random_mode_uses_probability_for_send_and_llm_only_for_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_burst(
        monkeypatch,
        decisions=('{"send_follow_up":true,"message":"second"}',),
        follow_up_turns=(text_turn("second", accepted=("second",)),),
        maximum=2,
        decision_mode="random",
        random_probability=80,
        random_decay=20,
        random_values=(0.79, 0.80),
    )

    assert result.sent_messages == ["initial", "second"]
    assert result.events.count("random-followup") == 1
    assert result.controller_inputs == []
    assert len(result.message_controller_inputs) == 1


@pytest.mark.parametrize(
    ("status", "expected_controller", "expected_history"),
    (
        (DispatchStatus.COMPLETE, 1, 1),
        (DispatchStatus.PARTIAL, 0, 1),
        (DispatchStatus.FAILED, 0, 0),
    ),
)
def test_red_initial_status_controls_controller_and_history(
    monkeypatch: pytest.MonkeyPatch,
    status: DispatchStatus,
    expected_controller: int,
    expected_history: int,
) -> None:
    accepted = () if status is DispatchStatus.FAILED else ("initial",)
    intended = 2 if status is DispatchStatus.PARTIAL else 1
    result = run_burst(
        monkeypatch,
        initial_turn=text_turn("initial", accepted=accepted, intended=intended),
        decisions=('{"send_follow_up":false,"message":""}',),
        context_job="" if status is DispatchStatus.FAILED else "context-1",
    )
    assert (
        result.events.count("followup"),
        result.events.count("history"),
    ) == (expected_controller, expected_history), (
        f"PCF-03 RED: initial {status.value} must select the exact controller/history path"
    )


@pytest.mark.parametrize(
    "decision",
    (
        '{"send_follow_up":false,"message":""}',
        '```json {"send_follow_up":true,"message":"rejected"}```',
        '{"send_follow_up":true,"message":"initial"}',
        OSError("controller unavailable"),
    ),
)
def test_red_controller_stop_matrix_preserves_successful_initial(
    monkeypatch: pytest.MonkeyPatch, decision: str | BaseException
) -> None:
    result = run_burst(monkeypatch, decisions=(decision,), maximum=3)
    assert result.completed, (
        "PCF-03 RED: false/malformed/duplicate/controller fault must not fail the initial"
    )
    assert len(result.controller_inputs) == 1 and result.sent_messages == ["initial"], (
        "PCF-03 RED: every controller stop reason must prevent all future sends"
    )
    assert len(result.history_payloads) == 1


@pytest.mark.parametrize(
    "follow_status", (DispatchStatus.PARTIAL, DispatchStatus.FAILED)
)
def test_red_noncomplete_follow_up_stops_without_failing_initial(
    monkeypatch: pytest.MonkeyPatch, follow_status: DispatchStatus
) -> None:
    accepted = ("accepted part",) if follow_status is DispatchStatus.PARTIAL else ()
    intended = 2 if follow_status is DispatchStatus.PARTIAL else 1
    result = run_burst(
        monkeypatch,
        decisions=('{"send_follow_up":true,"message":"second"}',),
        follow_up_turns=(text_turn("second", accepted=accepted, intended=intended),),
        maximum=3,
    )
    assert result.completed, (
        f"PCF-03 RED: {follow_status.value} follow-up must preserve initial success"
    )
    assert len(result.controller_inputs) == 1 and result.sent_messages == [
        "initial",
        "second",
    ], "PCF-03 RED: partial/failed follow-up must stop the controller loop"


@pytest.mark.parametrize(("configured", "expected"), ((-4, 0), (0, 0), (1, 1), (8, 3)))
def test_red_follow_up_maximum_is_clamped_zero_through_three(
    monkeypatch: pytest.MonkeyPatch, configured: int, expected: int
) -> None:
    messages = tuple(f"follow-{index}" for index in range(expected))
    decisions = tuple(
        f'{{"send_follow_up":true,"message":"{message}"}}' for message in messages
    )
    turns = tuple(text_turn(message, accepted=(message,)) for message in messages)
    result = run_burst(
        monkeypatch,
        decisions=decisions,
        follow_up_turns=turns,
        maximum=configured,
    )
    assert len(result.controller_inputs) == expected, (
        "PCF-03 RED: configured maximum must clamp to exactly 0..3 controller calls"
    )
    assert len(result.sent_messages) == expected + 1


def test_red_history_fault_cannot_duplicate_or_skip_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_burst(monkeypatch, history_fault=True, maximum=0)
    assert result.completed, (
        "PCF-03 RED: history failure must remain nonfatal after accepted dispatch"
    )
    assert result.events == ["initial", "finalize", "history", "cleanup"], (
        "PCF-03 RED: history failure must leave finalization and cleanup exactly once"
    )


def test_red_history_receives_one_immutable_accepted_turn_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_burst(
        monkeypatch,
        decisions=('{"send_follow_up":true,"message":"second"}',),
        follow_up_turns=(text_turn("second", accepted=("second",)),),
    )
    payload = result.history_payloads[0]
    assert isinstance(payload, tuple) and all(
        isinstance(turn, AcceptedTurn) for turn in payload
    ), "PCF-03 RED: history input must be one immutable AcceptedTurn tuple"
    assert [turn.message for turn in payload] == ["initial", "second"], (
        "PCF-03 RED: history must exclude controller JSON and retain accepted order"
    )
    assert result.controller_inputs == [(payload[0],)]


def test_red_history_pair_contains_one_text_part_per_accepted_logical_turn() -> None:
    initial = make_accepted_turn(
        "spoken and shown",
        (
            AcceptedComponent(AcceptedComponentKind.TTS, "/tmp/voice.wav"),
            AcceptedComponent(AcceptedComponentKind.TEXT, "spoken and shown"),
        ),
        intended_components=2,
    )
    partial = text_turn("accepted rejected", accepted=("accepted ",), intended=2)
    turns = (initial, partial)
    user, assistant = chat_executor._marked_history_pair("prompt", turns, "marker")
    assert user["content"] == [{"type": "text", "text": "prompt"}], (
        "PCF-03 RED: history must contain the original prompt exactly once"
    )
    assert assistant["content"] == [
        {"type": "text", "text": "spoken and shown"},
        {"type": "text", "text": "accepted "},
    ], "PCF-03 RED: history must store accepted logical turns without rejected segments"
