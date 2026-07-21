from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest
from astrbot_plugin_proactive_chat.core import context_predictor, proactive_prompt
from astrbot_plugin_proactive_chat.core.delivery import (
    AcceptedComponent,
    AcceptedComponentKind,
    DeliveryCoordinatorRegistry,
    GateVerdict,
    make_accepted_turn,
)


def test_context_analysis_rules_are_in_stable_system_prefix() -> None:
    async def scenario() -> None:
        captured: dict[str, str] = {}

        async def get_provider(_session_id: str) -> str:
            return "provider"

        async def generate(**kwargs):
            captured.update(
                {
                    "system_prompt": kwargs["system_prompt"],
                    "prompt": kwargs["prompt"],
                }
            )
            return SimpleNamespace(
                completion_text=(
                    '{"should_schedule":false,"delay_minutes":0,'
                    '"reason":"無","message_hint":""}'
                )
            )

        context = SimpleNamespace(
            get_current_chat_provider_id=get_provider,
            llm_generate=generate,
        )
        await context_predictor.predict_proactive_timing(
            context=context,
            session_id="platform:FriendMessage:42",
            last_message="我正在看電影",
            history=[],
            current_time_str="2026年07月21日 12:00",
            config={},
            persona_system_prompt="固定人格提示",
        )

        assert "固定人格提示" in captured["system_prompt"]
        assert context_predictor.PREDICT_TIMING_SYSTEM in captured["system_prompt"]
        assert "我正在看電影" not in captured["system_prompt"]
        assert context_predictor.PREDICT_TIMING_SYSTEM not in captured["prompt"]

    anyio.run(scenario)


def test_proactive_operation_rules_are_in_stable_system_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        plugin = SimpleNamespace(
            context=SimpleNamespace(),
            last_message_times={},
            session_data={},
            timezone=None,
            _pending_context_tasks={},
            _find_habit_task=lambda *_args: None,
        )
        session_config = {
            "proactive_prompt": "固定主動聊天規則",
            "context_aware_settings": {"enable_memory": False},
            "auto_check_settings": {"enable": True},
        }
        captured: dict[str, str] = {}

        async def prepare(*_args):
            return {"conv_id": "conv", "history": [], "system_prompt": "persona"}

        async def truncate(*_args):
            return []

        async def call(_context, _session, prompt, _history, system_prompt, **_kwargs):
            captured["prompt"] = prompt
            captured["system_prompt"] = system_prompt
            return SimpleNamespace(
                completion_text='{"send_message":false,"message":"","next_check_minutes":30}'
            )

        monkeypatch.setattr(proactive_prompt, "safe_prepare_llm_request", prepare)
        monkeypatch.setattr(
            proactive_prompt, "truncate_history_for_proactive_llm", truncate
        )
        monkeypatch.setattr(proactive_prompt, "call_llm", call)

        result = await proactive_prompt.prepare_and_call_auto_check(
            plugin, "platform:FriendMessage:42", session_config, 0, ""
        )

        assert result is not None
        assert proactive_prompt._AUTO_CHECK_PROMPT in captured["system_prompt"]
        assert "固定主動聊天規則" in captured["prompt"]
        assert "固定主動聊天規則" not in captured["system_prompt"]

    anyio.run(scenario)


def test_follow_up_rules_are_in_stable_system_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        registry = DeliveryCoordinatorRegistry()
        gate = registry.snapshot("platform:FriendMessage:42")
        plugin = SimpleNamespace(
            _gate_verdict=lambda _gate: GateVerdict.CURRENT,
            config={},
            context=SimpleNamespace(),
            session_data={},
        )
        initial = make_accepted_turn(
            "initial",
            (AcceptedComponent(AcceptedComponentKind.TEXT, "initial"),),
            intended_components=1,
        )
        captured: dict[str, str] = {}

        async def prepare(*_args):
            return {
                "conv_id": "conv",
                "history": [],
                "system_prompt": "persona",
            }

        async def truncate(_context, _session_id, history):
            return history

        async def call(_context, _session, prompt, _history, system_prompt, **_kwargs):
            captured["prompt"] = prompt
            captured["system_prompt"] = system_prompt
            return SimpleNamespace(
                completion_text='{"send_follow_up":false,"message":""}'
            )

        monkeypatch.setattr(proactive_prompt, "safe_prepare_llm_request", prepare)
        monkeypatch.setattr(
            proactive_prompt, "truncate_history_for_proactive_llm", truncate
        )
        monkeypatch.setattr(proactive_prompt, "call_llm", call)

        await proactive_prompt.request_follow_up_decision(
            plugin, "platform:FriendMessage:42", (initial,), gate
        )

        assert "已完成回覆的聊天" in captured["system_prompt"]
        assert "initial" in captured["prompt"]
        assert "initial" not in captured["system_prompt"]

    anyio.run(scenario)
