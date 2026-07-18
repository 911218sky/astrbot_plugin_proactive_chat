from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest

from astrbot_plugin_proactive_chat.core import proactive_prompt
from astrbot_plugin_proactive_chat.core.delivery import (
    AcceptedComponent,
    AcceptedComponentKind,
    DeliveryCoordinatorRegistry,
    GateVerdict,
    make_accepted_turn,
)


def test_follow_up_requests_keep_history_prefix_stable(
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
        captured_contexts: list[list] = []
        captured_prompts: list[str] = []
        initial = make_accepted_turn(
            "initial",
            (AcceptedComponent(AcceptedComponentKind.TEXT, "initial"),),
            intended_components=1,
        )
        follow_up = make_accepted_turn(
            "follow-up",
            (AcceptedComponent(AcceptedComponentKind.TEXT, "follow-up"),),
            intended_components=1,
        )

        async def prepare(*_args):
            return {
                "conv_id": "conv",
                "history": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "hello"}],
                    }
                ],
                "system_prompt": "persona",
            }

        async def truncate(_context, _session_id, history):
            return history

        async def call(*args, **_kwargs):
            captured_contexts.append(args[3])
            captured_prompts.append(args[2])
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
        await proactive_prompt.request_follow_up_decision(
            plugin, "platform:FriendMessage:42", (initial, follow_up), gate
        )

        assert captured_contexts == [captured_contexts[0], captured_contexts[0]]
        assert '"initial"' in captured_prompts[0]
        assert '"follow-up"' in captured_prompts[1]

    anyio.run(scenario)
