from __future__ import annotations

from types import SimpleNamespace
from typing import Literal, assert_never

import anyio
import pytest

from astrbot_plugin_proactive_chat.core import chat_executor
from astrbot_plugin_proactive_chat.core.delivery import (
    AcceptedComponent,
    AcceptedComponentKind,
    AcceptedTurn,
    DeliveryCoordinatorRegistry,
    GateVerdict,
    make_accepted_turn,
)

GatePhase = Literal["before_initial", "after_segment", "delay", "controller"]
GateTransition = Literal["activity", "disabled", "quiet"]


def text_turn(
    message: str,
    *,
    accepted: tuple[str, ...] = ("accepted",),
    intended: int = 1,
    verdict: GateVerdict = GateVerdict.CURRENT,
) -> AcceptedTurn:
    return make_accepted_turn(
        message,
        tuple(AcceptedComponent(AcceptedComponentKind.TEXT, text) for text in accepted),
        intended_components=intended,
        verdict=verdict,
    )


def run_burst(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_turn: AcceptedTurn | None = None,
    decisions: tuple[str | BaseException, ...] = (),
    follow_up_turns: tuple[AcceptedTurn, ...] = (),
    maximum: int = 1,
    history_fault: bool = False,
    context_job: str = "context-1",
) -> SimpleNamespace:
    async def scenario() -> SimpleNamespace:
        session_id = "platform:FriendMessage:42"
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(session_id)
        events: list[str] = []
        sent_messages: list[str] = []
        controller_inputs: list[tuple[AcceptedTurn, ...]] = []
        history_payloads: list[object] = []
        turns = list(
            (initial_turn or text_turn("initial", accepted=("initial",)),)
            + follow_up_turns
        )
        queued_decisions = list(decisions)

        async def dispatch(*, text: str, **_kwargs) -> AcceptedTurn:
            sent_messages.append(text)
            if len(sent_messages) == 1:
                events.append("initial")
            return turns.pop(0)

        async def finalize(*_args, **_kwargs) -> bool:
            events.append("finalize")
            return True

        async def controller(_plugin, _session_id, accepted, _gate) -> str | None:
            events.append("followup")
            controller_inputs.append(accepted)
            raw = queued_decisions.pop(0)
            if isinstance(raw, BaseException):
                raise raw
            return raw

        async def save_history(*args, **_kwargs) -> bool:
            events.append("history")
            history_payloads.append(args[4])
            if history_fault:
                raise OSError("history unavailable")
            return True

        async def cleanup(*_args, **_kwargs) -> None:
            events.append("cleanup")

        async def clear_failed(*_args, **_kwargs) -> bool:
            return True

        patches = (
            ("dispatch_proactive_message", dispatch),
            ("_update_unanswered_and_reschedule", finalize),
            ("_save_conversation_history", save_history),
            ("_request_follow_up_decision", controller),
            ("_cleanup_context_task", cleanup),
            ("_clear_regular_job_state_if_current", clear_failed),
        )
        for name, value in patches:
            monkeypatch.setattr(chat_executor, name, value)
        plugin = SimpleNamespace(
            config={},
            context=SimpleNamespace(),
            session_data={},
            last_bot_message_time=0.0,
            _find_habit_task=lambda *_args: None,
            _gate_verdict=lambda current: registry.verdict(
                current, enabled=True, quiet_hours=False
            ),
            _reset_group_silence_timer=lambda *_args: None,
        )
        completed = await chat_executor._deliver_and_finalize(
            plugin,
            session_id,
            {
                "immediate_follow_up_settings": {
                    "enable": True,
                    "max_follow_ups": maximum,
                    "delay_seconds": 0,
                }
            },
            "initial",
            "conversation",
            "original prompt",
            0,
            context_job,
            gate,
        )
        return SimpleNamespace(
            completed=completed,
            events=events,
            sent_messages=sent_messages,
            controller_inputs=controller_inputs,
            history_payloads=history_payloads,
        )

    return anyio.run(scenario)


def run_gate_case(
    monkeypatch: pytest.MonkeyPatch,
    phase: GatePhase,
    transition: GateTransition,
    *,
    aliases: bool = False,
) -> tuple[tuple[int, int, int, int, int], int]:
    async def scenario() -> tuple[tuple[int, int, int, int, int], int]:
        alias = "old:FriendMessage:42"
        canonical = "new:FriendMessage:42" if aliases else alias
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(alias, canonical)
        quiet = False
        enabled = True
        sends = controllers = histories = finalizations = quiet_reschedules = 0
        release = anyio.Event()
        send_signal, receive_signal = anyio.create_memory_object_stream[str](2)

        def verdict() -> GateVerdict:
            return registry.verdict(gate, enabled=enabled, quiet_hours=quiet)

        def apply_transition() -> None:
            nonlocal quiet, enabled
            match transition:
                case "activity":
                    registry.record_activity(alias, canonical)
                case "disabled":
                    enabled = False
                case "quiet":
                    quiet = True
                case unreachable:
                    assert_never(unreachable)

        if phase == "before_initial":
            apply_transition()

        async def dispatch(*, text: str, **_kwargs) -> AcceptedTurn:
            nonlocal sends
            current = verdict()
            if current is not GateVerdict.CURRENT:
                return make_accepted_turn(
                    text, (), intended_components=1, verdict=current
                )
            sends += 1
            component = AcceptedComponent(AcceptedComponentKind.TEXT, text)
            if phase == "after_segment":
                apply_transition()
                return make_accepted_turn(
                    text, (component,), intended_components=2, verdict=verdict()
                )
            return make_accepted_turn(text, (component,), intended_components=1)

        async def finalize(*_args, **_kwargs) -> bool:
            nonlocal finalizations
            if verdict() in (GateVerdict.ACTIVITY_CHANGED, GateVerdict.DISABLED):
                return False
            finalizations += 1
            return True

        async def controller(*_args, **_kwargs) -> str:
            nonlocal controllers
            controllers += 1
            await send_signal.send("controller")
            await release.wait()
            return '{"send_follow_up":true,"message":"future"}'

        async def delay(_seconds: float) -> None:
            await send_signal.send("delay")
            await release.wait()

        async def save_history(*_args, **_kwargs) -> bool:
            nonlocal histories
            histories += 1
            return True

        async def reschedule(*_args, **_kwargs) -> None:
            nonlocal quiet_reschedules
            quiet_reschedules += 1

        monkeypatch.setattr(chat_executor, "dispatch_proactive_message", dispatch)
        monkeypatch.setattr(
            chat_executor, "_update_unanswered_and_reschedule", finalize
        )
        monkeypatch.setattr(chat_executor, "_save_conversation_history", save_history)
        monkeypatch.setattr(chat_executor, "_request_follow_up_decision", controller)
        if phase == "delay":
            monkeypatch.setattr(chat_executor.asyncio, "sleep", delay)
        plugin = SimpleNamespace(
            config={},
            context=SimpleNamespace(),
            session_data={},
            last_bot_message_time=0.0,
            _find_habit_task=lambda *_args: None,
            _gate_verdict=lambda _gate: verdict(),
            _reset_group_silence_timer=lambda *_args: None,
            _retry_chat_job=reschedule,
        )

        async def run() -> None:
            try:
                await chat_executor._deliver_and_finalize(
                    plugin,
                    canonical,
                    {
                        "immediate_follow_up_settings": {
                            "enable": True,
                            "max_follow_ups": 1,
                            "delay_seconds": int(phase == "delay"),
                        }
                    },
                    "initial",
                    "conversation",
                    "prompt",
                    0,
                    "",
                    gate,
                )
            finally:
                await send_signal.send("done")

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run)
            signal = await receive_signal.receive()
            if signal != "done":
                apply_transition()
                release.set()
                assert await receive_signal.receive() == "done"
        counts = (
            sends,
            controllers,
            histories,
            finalizations,
            quiet_reschedules,
        )
        return counts, registry.coordinator_count

    return anyio.run(scenario)
