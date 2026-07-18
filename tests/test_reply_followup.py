from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest

from astrbot_plugin_proactive_chat import main
from astrbot_plugin_proactive_chat.core.delivery import (
    AcceptedComponentKind,
    DeliveryCoordinatorRegistry,
    GateVerdict,
)


class Result:
    def __init__(self, *, llm: bool, text: str = "reply") -> None:
        self.chain = [SimpleNamespace(text=text)] if text else []
        self._llm = llm

    def is_llm_result(self) -> bool:
        return self._llm


class Event:
    unified_msg_origin = "platform:FriendMessage:42"

    def __init__(self, result) -> None:
        self._result = result

    def get_result(self):
        return self._result


def make_plugin() -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(),
        context=SimpleNamespace(),
        session_data={},
        session_temp_state={},
        _cleanup_counter=0,
        _reply_follow_up_tasks={},
        _delivery_coordinators=DeliveryCoordinatorRegistry(),
        _canonical_delivery_session=lambda session_id: session_id,
        _cancel_reply_follow_up_task=lambda session_id: None,
        _accepted_turn_from_result=main.ProactiveChatPlugin._accepted_turn_from_result,
    )


def test_after_message_sent_schedules_only_llm_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        plugin = make_plugin()
        calls = []

        async def run_follow_ups(session_id, config, gate, turn) -> None:
            calls.append((session_id, config, gate, turn))

        plugin._run_reply_follow_ups = run_follow_ups
        monkeypatch.setattr(main, "get_session_config", lambda *_args: {"enable": True})

        await main.ProactiveChatPlugin.on_after_message_sent(
            plugin, Event(Result(llm=True))
        )
        task = next(iter(plugin._reply_follow_up_tasks.values()))
        await task
        assert len(calls) == 1
        assert calls[0][0] == Event.unified_msg_origin
        assert calls[0][3].message == "reply"
        assert calls[0][3].accepted_components[0].kind is AcceptedComponentKind.TEXT

        await main.ProactiveChatPlugin.on_after_message_sent(
            plugin, Event(Result(llm=False))
        )
        assert len(calls) == 1

    anyio.run(scenario)


def test_after_message_sent_debounces_rapid_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        plugin = make_plugin()
        plugin._cancel_reply_follow_up_task = lambda session_id: (
            main.ProactiveChatPlugin._cancel_reply_follow_up_task(plugin, session_id)
        )
        calls = []

        async def run_follow_ups(_session_id, _config, _gate, turn) -> None:
            calls.append(turn.message)

        plugin._run_reply_follow_ups = run_follow_ups
        monkeypatch.setattr(main, "get_session_config", lambda *_args: {"enable": True})

        await main.ProactiveChatPlugin.on_after_message_sent(
            plugin, Event(Result(llm=True, text="first"))
        )
        await main.ProactiveChatPlugin.on_after_message_sent(
            plugin, Event(Result(llm=True, text="last"))
        )
        await anyio.sleep(0)

        assert calls == ["last"]

    anyio.run(scenario)


def test_after_message_sent_ignores_missing_or_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        plugin = make_plugin()
        plugin._run_reply_follow_ups = pytest.fail
        monkeypatch.setattr(main, "get_session_config", lambda *_args: {"enable": True})
        await main.ProactiveChatPlugin.on_after_message_sent(plugin, Event(None))
        await main.ProactiveChatPlugin.on_after_message_sent(
            plugin, Event(Result(llm=True, text=""))
        )
        assert plugin._reply_follow_up_tasks == {}

    anyio.run(scenario)


def test_reply_follow_up_task_uses_gate_and_does_not_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        plugin = make_plugin()
        gate = plugin._delivery_coordinators.record_activity(Event.unified_msg_origin)
        calls = []

        async def collect(*args, **kwargs):
            calls.append((args, kwargs))
            assert plugin._gate_verdict(gate) is GateVerdict.CURRENT

        plugin._gate_verdict = lambda current: plugin._delivery_coordinators.verdict(
            current, enabled=True, quiet_hours=False
        )
        monkeypatch.setattr(main.chat_executor, "collect_follow_ups", collect)
        await main.ProactiveChatPlugin._run_reply_follow_ups(
            plugin,
            Event.unified_msg_origin,
            {"enable": True},
            gate,
            main.ProactiveChatPlugin._accepted_turn_from_result(Result(llm=True)),
        )
        assert len(calls) == 1
        assert calls[0][0][1] == Event.unified_msg_origin

    anyio.run(scenario)
