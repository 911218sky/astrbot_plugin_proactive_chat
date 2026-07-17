from __future__ import annotations

from types import SimpleNamespace
from collections.abc import Callable
from typing import Literal, assert_never

import anyio
import pytest

from astrbot_plugin_proactive_chat.core import chat_executor, messaging, send
from astrbot_plugin_proactive_chat.core.delivery import (
    AcceptedComponent,
    AcceptedComponentKind,
    AcceptedTurn,
    DeliveryCoordinatorRegistry,
    DispatchGate,
    GateVerdict,
    make_accepted_turn,
)
from astrbot_plugin_proactive_chat.main import ProactiveChatPlugin

Transition = Literal["activity", "disabled", "quiet"]
Boundary = Literal["provider", "tts", "hook", "pre_finalization"]


BoundaryRunner = Callable[[Boundary, Transition], tuple[int, int, int]]
MainBarrierRunner = Callable[[bool], tuple[bool, bool, bool, int]]
HistoryRunner = Callable[[Literal["delay", "write"]], tuple[bool, int, bool]]


class GateState:
    def __init__(self, session_id: str) -> None:
        self.registry = DeliveryCoordinatorRegistry()
        self.gate = self.registry.record_activity(session_id)
        self.enabled = True
        self.quiet = False

    def verdict(self, gate: DispatchGate | None = None) -> GateVerdict:
        return self.registry.verdict(
            gate or self.gate, enabled=self.enabled, quiet_hours=self.quiet
        )


@pytest.fixture
def boundary_runner(monkeypatch: pytest.MonkeyPatch) -> BoundaryRunner:
    async def scenario(
        boundary: Boundary, transition: Transition
    ) -> tuple[int, int, int]:
        session_id = "platform:GroupMessage:42"
        state = GateState(session_id)
        entered, release = anyio.Event(), anyio.Event()
        accepted = sends = finalizations = 0

        async def send_message(*_args, **_kwargs) -> bool:
            nonlocal sends
            sends += 1
            return True

        async def invoke_dispatch() -> None:
            nonlocal accepted
            settings = {
                "tts_settings": {"enable_tts": boundary == "tts"},
                "segmented_reply_settings": {"enable": False},
            }
            monkeypatch.setattr(send, "get_session_config", lambda *_args: settings)

            async def get_audio(_text: str) -> str:
                entered.set()
                await release.wait()
                return "/tmp/not-sent.wav"

            async def hooks(_sid, components, _context, _data):
                entered.set()
                await release.wait()
                return components

            provider = SimpleNamespace(get_audio=get_audio)
            monkeypatch.setattr(send, "get_tts_provider", lambda *_args: provider)
            if boundary == "hook":
                monkeypatch.setattr(messaging, "trigger_decorating_hooks", hooks)
            turn = await send.dispatch_proactive_message(
                session_id=session_id,
                text="reply",
                config=SimpleNamespace(),
                context=SimpleNamespace(send_message=send_message),
                session_data={},
                gate_check=state.verdict,
            )
            accepted = len(turn.accepted_components)

        async def invoke_provider() -> None:
            async def preconditions(*_args, **_kwargs):
                return {"enable": True}, 0, False

            async def provider(*_args, **_kwargs):
                entered.set()
                await release.wait()
                return "reply", "conversation", "prompt", None

            async def deliver(*_args, **_kwargs) -> bool:
                nonlocal sends
                sends += 1
                return True

            monkeypatch.setattr(chat_executor, "_check_preconditions", preconditions)
            monkeypatch.setattr(chat_executor, "_prepare_and_call_llm", provider)
            monkeypatch.setattr(chat_executor, "_deliver_and_finalize", deliver)
            plugin = SimpleNamespace(
                _find_habit_task=lambda *_args: None,
                _delivery_coordinators=state.registry,
                _gate_verdict=state.verdict,
            )
            await chat_executor.check_and_chat(plugin, session_id, gate=state.gate)

        async def invoke_finalization() -> None:
            nonlocal accepted, sends, finalizations

            async def dispatch(**_kwargs) -> AcceptedTurn:
                nonlocal accepted, sends
                sends += 1
                entered.set()
                await release.wait()
                component = AcceptedComponent(AcceptedComponentKind.TEXT, "reply")
                turn = make_accepted_turn("reply", (component,), intended_components=1)
                accepted = 1
                return turn

            async def save_data() -> None:
                nonlocal finalizations
                finalizations += 1

            monkeypatch.setattr(chat_executor, "dispatch_proactive_message", dispatch)
            plugin = SimpleNamespace(
                config={},
                context=SimpleNamespace(),
                data_lock=anyio.Lock(),
                session_data={session_id: {"unanswered_count": 0}},
                _find_habit_task=lambda *_args: None,
                _gate_verdict=state.verdict,
                _reset_group_silence_timer=lambda *_args: None,
                _save_data=save_data,
                timezone=None,
            )
            await chat_executor._deliver_and_finalize(
                plugin, session_id, {}, "reply", "", "prompt", 0, "", state.gate
            )

        action = {
            "provider": invoke_provider,
            "pre_finalization": invoke_finalization,
        }.get(boundary, invoke_dispatch)
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(action)
            await entered.wait()
            match transition:
                case "activity":
                    state.registry.record_activity(state.gate.canonical_session_id)
                case "disabled":
                    state.enabled = False
                case "quiet":
                    state.quiet = True
                case unreachable:
                    assert_never(unreachable)
            release.set()
        return accepted, sends, finalizations

    return lambda boundary, transition: anyio.run(scenario, boundary, transition)


@pytest.fixture
def main_barrier_runner(monkeypatch: pytest.MonkeyPatch) -> MainBarrierRunner:
    async def scenario(inject_activity: bool) -> tuple[bool, bool, bool, int]:
        alias, canonical = "old:FriendMessage:42", "new:FriendMessage:42"

        snapshot_calls: list[None] = []
        original_snapshot = DeliveryCoordinatorRegistry.snapshot

        def snapshot(
            registry: DeliveryCoordinatorRegistry,
            alias_session_id: str,
            canonical_session_id: str | None = None,
        ) -> DispatchGate:
            snapshot_calls.append(None)
            return original_snapshot(registry, alias_session_id, canonical_session_id)

        monkeypatch.setattr(DeliveryCoordinatorRegistry, "snapshot", snapshot)
        registry = DeliveryCoordinatorRegistry()
        registry.record_activity(alias)
        merge_entered, merge_release = anyio.Event(), anyio.Event()
        provider_calls = 0
        snapshot_before = lease_before = aliases_merged = False

        async def merge_state(_old: str, _new: str) -> None:
            nonlocal snapshot_before, lease_before, aliases_merged
            coordinator = registry.coordinator_for(alias)
            snapshot_before = len(snapshot_calls) == 1
            lease_before = coordinator.busy
            aliases_merged = coordinator is registry.coordinator_for(canonical)
            merge_entered.set()
            await merge_release.wait()

        async def executor(_plugin, _session_id, _ctx_job_id, *, gate) -> None:
            nonlocal provider_calls
            if (
                registry.verdict(gate, enabled=True, quiet_hours=False)
                is GateVerdict.CURRENT
            ):
                provider_calls += 1

        monkeypatch.setattr(
            chat_executor, "resolve_session_umo", lambda *_args: canonical
        )
        monkeypatch.setattr(chat_executor, "check_and_chat", executor)
        plugin = SimpleNamespace(
            _delivery_coordinators=registry,
            _merge_session_state=merge_state,
            _chat_run_semaphore=anyio.Lock(),
        )
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(ProactiveChatPlugin.check_and_chat, plugin, alias)
            await merge_entered.wait()
            if inject_activity:
                registry.record_activity(canonical)
            merge_release.set()
        return snapshot_before, lease_before, aliases_merged, provider_calls

    return lambda inject_activity: anyio.run(scenario, inject_activity)


@pytest.fixture
def history_runner(monkeypatch: pytest.MonkeyPatch) -> HistoryRunner:
    async def scenario(phase: Literal["delay", "write"]) -> tuple[bool, int, bool]:
        session_id = "platform:FriendMessage:42"
        state = GateState(session_id)
        entered, release, revision_signal_event = (anyio.Event() for _index in range(3))
        writes = 0

        watch_signals: list[None] = []

        def revision_signal(_gate: DispatchGate) -> anyio.Event:
            watch_signals.append(None)
            return revision_signal_event

        async def delay(_seconds: float) -> None:
            entered.set()
            await release.wait()

        async def add_message_pair(**_kwargs) -> None:
            nonlocal writes
            if phase == "write":
                entered.set()
                await release.wait()
                await anyio.lowlevel.checkpoint()
            writes += 1

        async def settle_marker(*_args, **_kwargs) -> None:
            return None

        if phase == "delay":
            monkeypatch.setattr(chat_executor.asyncio, "sleep", delay)
        monkeypatch.setattr(
            chat_executor, "_settle_history_marker_shielded", settle_marker
        )
        plugin = SimpleNamespace(
            config={},
            _delivery_coordinators=SimpleNamespace(revision_signal=revision_signal),
            _history_save_lock=anyio.Lock(),
            context=SimpleNamespace(
                conversation_manager=SimpleNamespace(add_message_pair=add_message_pair)
            ),
            _gate_verdict=state.verdict,
        )
        saved = False

        async def save_history() -> None:
            nonlocal saved
            saved = await chat_executor._save_conversation_history(
                plugin,
                {
                    "history_settings": {
                        "save_proactive_history": True,
                        "history_save_delay_seconds": int(phase == "delay"),
                        "history_save_retry_attempts": 0,
                    }
                },
                "conversation",
                "prompt",
                "response",
                state.gate,
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(save_history)
            await entered.wait()
            state.registry.record_activity(session_id)
            revision_signal_event.set()
            release.set()
        return saved, writes, bool(watch_signals)

    return lambda phase: anyio.run(scenario, phase)
