from __future__ import annotations

from types import SimpleNamespace

import anyio

from astrbot_plugin_proactive_chat.core import chat_executor
from astrbot_plugin_proactive_chat.core.config import get_context_analysis_provider_id
from astrbot_plugin_proactive_chat.core import llm_helpers
from astrbot_plugin_proactive_chat.core import proactive_prompt
from astrbot_plugin_proactive_chat.core.auto_check import AutoCheckDecision
from astrbot_plugin_proactive_chat.core.delivery import (
    DeliveryCoordinatorRegistry,
    GateVerdict,
)


def _plugin() -> SimpleNamespace:
    registry = DeliveryCoordinatorRegistry()
    gate = registry.snapshot("platform:FriendMessage:42")
    plugin = SimpleNamespace(
        _delivery_coordinators=registry,
        _gate_verdict=lambda _gate: GateVerdict.CURRENT,
        _find_habit_task=lambda *_args: None,
        _schedule_next_chat_and_save=None,
        _cleanup_context_task=lambda *_args: None,
        config={},
        context=SimpleNamespace(),
        session_data={},
    )
    plugin._test_gate = gate
    return plugin


def test_context_analysis_provider_prefers_top_level_setting() -> None:
    assert (
        get_context_analysis_provider_id(
            {"context_analysis_llm_provider_id": "global-provider"},
            {"context_aware_settings": {"llm_provider_id": "legacy-provider"}},
        )
        == "global-provider"
    )


def test_context_analysis_provider_uses_legacy_session_fallback() -> None:
    assert (
        get_context_analysis_provider_id(
            {"context_analysis_llm_provider_id": "  "},
            {"context_aware_settings": {"llm_provider_id": " legacy-provider "}},
        )
        == "legacy-provider"
    )


def test_auto_check_no_send_reschedules_without_delivery(monkeypatch) -> None:
    async def scenario() -> None:
        plugin = _plugin()
        scheduled: list[str] = []
        delivered: list[str] = []
        async def schedule(session_id: str) -> None:
            scheduled.append(session_id)

        plugin._schedule_next_chat_and_save = schedule

        async def preconditions(*_args, **_kwargs):
            return (
                {"_session_type": "private", "auto_check_settings": {"enable": True}},
                0,
                False,
            )

        async def auto_check(*_args):
            return AutoCheckDecision(False, ""), "conv", "prompt", None

        monkeypatch.setattr(chat_executor, "_check_preconditions", preconditions)
        monkeypatch.setattr(chat_executor, "_prepare_and_call_auto_check", auto_check)

        async def deliver(*_args):
            delivered.append("sent")
            return True

        monkeypatch.setattr(chat_executor, "_deliver_and_finalize", deliver)
        await chat_executor.check_and_chat(
            plugin, "platform:FriendMessage:42", gate=plugin._test_gate
        )
        assert scheduled == ["platform:FriendMessage:42"]
        assert delivered == []

    anyio.run(scenario)


def test_auto_check_send_uses_existing_delivery_pipeline(monkeypatch) -> None:
    async def scenario() -> None:
        plugin = _plugin()
        delivered: list[tuple] = []

        async def preconditions(*_args, **_kwargs):
            return (
                {"_session_type": "private", "auto_check_settings": {"enable": True}},
                0,
                False,
            )

        async def auto_check(*_args):
            return AutoCheckDecision(True, "想你了"), "conv", "prompt", None

        monkeypatch.setattr(chat_executor, "_check_preconditions", preconditions)
        monkeypatch.setattr(chat_executor, "_prepare_and_call_auto_check", auto_check)

        async def deliver(*args) -> bool:
            delivered.append(args)
            return True

        monkeypatch.setattr(chat_executor, "_deliver_and_finalize", deliver)
        await chat_executor.check_and_chat(
            plugin, "platform:FriendMessage:42", gate=plugin._test_gate
        )
        assert delivered
        assert delivered[0][3] == "想你了"

    anyio.run(scenario)


def test_habit_auto_check_no_send_keeps_single_habit_schedule(monkeypatch) -> None:
    async def scenario() -> None:
        plugin = _plugin()
        scheduled: list[str] = []
        cleaned: list[str] = []
        plugin._find_habit_task = lambda *_args: {"count_unanswered": False}

        async def schedule(session_id: str) -> None:
            scheduled.append(session_id)

        async def cleanup(session_id: str, job_id: str) -> None:
            cleaned.append(f"{session_id}:{job_id}")

        plugin._schedule_next_habit_task = schedule
        plugin._cleanup_habit_task = cleanup

        async def preconditions(*_args, **_kwargs):
            return (
                {
                    "_session_type": "private",
                    "auto_check_settings": {"enable": True},
                },
                0,
                False,
            )

        async def auto_check(*_args):
            return AutoCheckDecision(False, ""), "conv", "prompt", None

        monkeypatch.setattr(chat_executor, "_check_preconditions", preconditions)
        monkeypatch.setattr(chat_executor, "_prepare_and_call_auto_check", auto_check)
        await chat_executor.check_and_chat(
            plugin,
            "platform:FriendMessage:42",
            ctx_job_id="habit_platform:FriendMessage:42_1",
            gate=plugin._test_gate,
        )

        assert scheduled == ["platform:FriendMessage:42"]
        assert cleaned == ["platform:FriendMessage:42:habit_platform:FriendMessage:42_1"]

    anyio.run(scenario)


def test_auto_check_uses_context_analysis_provider(monkeypatch) -> None:
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
            "proactive_prompt": "請自然關心對方",
            "context_aware_settings": {"llm_provider_id": "context-provider"},
            "auto_check_settings": {"enable": True, "profile": "romantic"},
        }
        calls: list[str | None] = []

        async def prepare(*_args):
            return {"conv_id": "conv", "history": [], "system_prompt": "persona"}

        async def memory(*_args):
            return "persona"

        async def truncate(*_args):
            return []

        async def call(*_args, **kwargs):
            calls.append(kwargs.get("provider_id"))
            return SimpleNamespace(
                completion_text='{"send_message":false,"message":""}'
            )

        monkeypatch.setattr(proactive_prompt, "safe_prepare_llm_request", prepare)
        monkeypatch.setattr(proactive_prompt, "inject_memory", memory)
        monkeypatch.setattr(
            proactive_prompt, "truncate_history_for_proactive_llm", truncate
        )
        monkeypatch.setattr(proactive_prompt, "call_llm", call)
        result = await proactive_prompt.prepare_and_call_auto_check(
            plugin, "platform:FriendMessage:42", session_config, 0, ""
        )
        assert result is not None
        assert result[0].send_message is False
        assert calls == ["context-provider"]

    anyio.run(scenario)


def test_llm_fallback_keeps_explicit_context_provider() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        class Provider:
            async def text_chat(self, **_kwargs):
                calls.append("selected")
                return "fallback"

        async def generate(**_kwargs):
            raise RuntimeError("temporary provider failure")

        def unexpected_default(**_kwargs):
            raise AssertionError("fallback must not switch provider")

        context = SimpleNamespace(
            llm_generate=generate,
            get_provider_by_id=lambda provider_id: Provider()
            if provider_id == "context-provider"
            else None,
            get_using_provider=unexpected_default,
            get_current_chat_provider_id=lambda _session_id: "default",
        )
        result = await llm_helpers.call_llm(
            context,
            "platform:FriendMessage:42",
            "prompt",
            [],
            "system",
            provider_id="context-provider",
        )
        assert result == "fallback"
        assert calls == ["selected"]

    anyio.run(scenario)
