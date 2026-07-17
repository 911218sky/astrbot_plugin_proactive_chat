from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest

from astrbot_plugin_proactive_chat.core import chat_executor
from astrbot_plugin_proactive_chat.core.delivery import (
    DeliveryCoordinatorRegistry,
    GateVerdict,
)
from astrbot_plugin_proactive_chat.main import ProactiveChatPlugin


def _plugin(registry: DeliveryCoordinatorRegistry) -> SimpleNamespace:
    return SimpleNamespace(
        _delivery_coordinators=registry,
        _chat_run_semaphore=anyio.Lock(),
    )


def test_terminal_run_reclaims_coordinator_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(fail: bool) -> int:
        session_id = "platform:FriendMessage:42"
        registry = DeliveryCoordinatorRegistry()
        registry.record_activity(session_id)

        async def executor(*_args, **_kwargs) -> None:
            if fail:
                raise OSError("provider failed")

        monkeypatch.setattr(
            chat_executor, "resolve_session_umo", lambda *_args: session_id
        )
        monkeypatch.setattr(chat_executor, "check_and_chat", executor)
        if fail:
            with pytest.raises(OSError, match="provider failed"):
                await ProactiveChatPlugin.check_and_chat(_plugin(registry), session_id)
        else:
            await ProactiveChatPlugin.check_and_chat(_plugin(registry), session_id)
        return registry.coordinator_count

    assert anyio.run(scenario, False) == 0
    assert anyio.run(scenario, True) == 0


def test_cancelled_run_reclaims_terminal_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[bool, int]:
        session_id = "platform:FriendMessage:42"
        registry = DeliveryCoordinatorRegistry()
        registry.record_activity(session_id)
        entered = anyio.Event()

        async def executor(*_args, **_kwargs) -> None:
            entered.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(
            chat_executor, "resolve_session_umo", lambda *_args: session_id
        )
        monkeypatch.setattr(chat_executor, "check_and_chat", executor)
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    ProactiveChatPlugin.check_and_chat,
                    _plugin(registry),
                    session_id,
                )
                await entered.wait()
                scope.cancel()
        return scope.cancelled_caught, registry.coordinator_count

    assert anyio.run(scenario) == (True, 0)


def test_cancelled_waiter_is_reclaimed_after_stale_owner_releases() -> None:
    async def scenario() -> int:
        session_id = "platform:FriendMessage:42"
        registry = DeliveryCoordinatorRegistry()
        stale_gate = registry.record_activity(session_id)
        coordinator = registry.coordinator_for(session_id)
        owner_entered = anyio.Event()
        waiter_attempted = anyio.Event()
        release_owner = anyio.Event()
        waiter_scope = anyio.CancelScope()

        async def owner() -> None:
            async with coordinator.lease():
                owner_entered.set()
                await release_owner.wait()
            registry.retire(stale_gate)

        async def waiter(current_gate) -> None:
            try:
                with waiter_scope:
                    waiter_attempted.set()
                    async with coordinator.lease():
                        pass
            finally:
                registry.retire(current_gate)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(owner)
            await owner_entered.wait()
            current_gate = registry.record_activity(session_id)
            task_group.start_soon(waiter, current_gate)
            await waiter_attempted.wait()
            await anyio.lowlevel.checkpoint()
            waiter_scope.cancel()
            await anyio.lowlevel.checkpoint()
            release_owner.set()
        return registry.coordinator_count

    assert anyio.run(scenario) == 0


def test_stale_run_cannot_reclaim_new_alias_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[GateVerdict, int, int]:
        alias = "old:FriendMessage:42"
        canonical = "new:FriendMessage:42"
        registry = DeliveryCoordinatorRegistry()
        registry.record_activity(alias)
        entered = anyio.Event()
        release = anyio.Event()
        stale_verdict = GateVerdict.CURRENT

        async def executor(_plugin, _session_id, _ctx_job_id, *, gate) -> None:
            nonlocal stale_verdict
            entered.set()
            await release.wait()
            stale_verdict = registry.verdict(gate, enabled=True, quiet_hours=False)

        monkeypatch.setattr(
            chat_executor, "resolve_session_umo", lambda *_args: canonical
        )
        monkeypatch.setattr(chat_executor, "check_and_chat", executor)
        plugin = _plugin(registry)

        async def merge_state(*_args) -> None:
            return None

        plugin._merge_session_state = merge_state
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(ProactiveChatPlugin.check_and_chat, plugin, alias)
            await entered.wait()
            current_gate = registry.record_activity(alias, canonical)
            release.set()
        remaining = registry.coordinator_count
        retired = int(registry.retire(current_gate))
        return stale_verdict, remaining, retired

    assert anyio.run(scenario) == (GateVerdict.ACTIVITY_CHANGED, 1, 1)


def test_disabled_inbound_message_reclaims_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> int:
        session_id = "platform:FriendMessage:42"
        registry = DeliveryCoordinatorRegistry()
        plugin = SimpleNamespace(
            _delivery_coordinators=registry,
            _canonical_delivery_session=lambda alias: alias,
            config={},
            last_message_times={},
            session_temp_state={},
            data_lock=anyio.Lock(),
            session_data={},
            plugin_start_time=0.0,
            first_message_logged=set(),
        )

        async def save_data() -> None:
            return None

        plugin._save_data = save_data
        event = SimpleNamespace(
            unified_msg_origin=session_id,
            get_messages=lambda: ["incoming"],
            get_self_id=lambda: "bot",
            message_str="incoming",
        )
        monkeypatch.setattr(
            "astrbot_plugin_proactive_chat.main.get_session_config",
            lambda *_args: {"enable": False},
        )
        await ProactiveChatPlugin._handle_message(plugin, event, is_group=False)
        return registry.coordinator_count

    assert anyio.run(scenario) == 0
