from __future__ import annotations

from dataclasses import FrozenInstanceError
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal

import anyio
import pytest
from aiosqlite import Connection

from astrbot_plugin_proactive_chat.core import chat_executor, send
from astrbot_plugin_proactive_chat.core.delivery import (
    AcceptedComponent,
    AcceptedComponentKind,
    DeliveryCoordinatorRegistry,
    DispatchGate,
    DispatchStatus,
    GateVerdict,
    make_accepted_turn,
)
from astrbot_plugin_proactive_chat.main import ProactiveChatPlugin
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.db.sqlite import SQLiteDatabase

if TYPE_CHECKING:
    from conftest import Boundary, BoundaryRunner, HistoryRunner, MainBarrierRunner


BusyMergeResult = tuple[RuntimeError | None, int, int, int]


@pytest.fixture
def text_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    outcomes: list[bool] = []
    settings = {
        "tts_settings": {"enable_tts": False},
        "segmented_reply_settings": {"enable": True},
    }
    monkeypatch.setattr(send, "get_session_config", lambda *_args: settings)
    monkeypatch.setattr(send, "split_text", lambda *_args: ["one", "two"])
    monkeypatch.setattr(send, "calc_segment_interval", lambda *_args: 0)

    async def dispatch(*_args, **_kwargs) -> bool:
        return outcomes.pop(0)

    monkeypatch.setattr(send, "send_chain_with_hooks", dispatch)
    return outcomes


@pytest.mark.parametrize(
    ("outcomes", "expected"), (((True, False), True), ((False,), False))
)
def test_legacy_boolean_dispatch_reports_any_accepted_component(
    text_dispatch: list[bool], outcomes: tuple[bool, ...], expected: bool
) -> None:
    text_dispatch.extend(outcomes)

    accepted = anyio.run(
        partial(
            send.send_proactive_message,
            session_id="platform:FriendMessage:42",
            text="one two",
            config=SimpleNamespace(),
            context=SimpleNamespace(),
            session_data={},
        )
    )
    assert accepted is expected


def test_gate_components_and_turn_are_frozen() -> None:
    gate = DispatchGate("platform:FriendMessage:42", 1)
    component = AcceptedComponent(AcceptedComponentKind.TEXT, "hello")
    turn = make_accepted_turn("hello", (component,), intended_components=1)
    assert turn.status is DispatchStatus.COMPLETE
    with pytest.raises(FrozenInstanceError):
        gate.revision = 2
    with pytest.raises(FrozenInstanceError):
        component.content = "changed"
    with pytest.raises(FrozenInstanceError):
        turn.message = "changed"


@pytest.mark.parametrize(
    ("accepted", "intended", "expected"),
    (
        (0, 2, DispatchStatus.FAILED),
        (1, 2, DispatchStatus.PARTIAL),
        (1, 1, DispatchStatus.COMPLETE),
    ),
)
def test_dispatch_status_counts_accepted_against_intended(
    accepted: int, intended: int, expected: DispatchStatus
) -> None:
    component = AcceptedComponent(AcceptedComponentKind.TEXT, "text")
    components = (component,) * accepted
    status = make_accepted_turn("text", components, intended_components=intended).status
    assert status is expected


@pytest.mark.parametrize(
    ("enabled", "quiet", "activity", "expected"),
    (
        (True, False, False, GateVerdict.CURRENT),
        (False, False, False, GateVerdict.DISABLED),
        (True, True, False, GateVerdict.QUIET_HOURS),
        (False, True, True, GateVerdict.ACTIVITY_CHANGED),
    ),
)
def test_gate_verdict_distinguishes_invalidation_causes(
    enabled: bool, quiet: bool, activity: bool, expected: GateVerdict
) -> None:
    registry = DeliveryCoordinatorRegistry()
    gate = registry.record_activity("platform:FriendMessage:42")
    if activity:
        registry.record_activity(gate.canonical_session_id)
    assert registry.verdict(gate, enabled=enabled, quiet_hours=quiet) is expected


def test_idle_alias_merge_retains_max_revision_and_one_lock() -> None:
    registry = DeliveryCoordinatorRegistry()
    registry.record_activity("old:FriendMessage:42")
    registry.record_activity("old:FriendMessage:42")
    registry.record_activity("new:FriendMessage:42")
    coordinator = registry.merge_aliases("old:FriendMessage:42", "new:FriendMessage:42")
    assert coordinator.revision == 2
    assert registry.coordinator_count == 1
    assert registry.coordinator_for("old:FriendMessage:42") is coordinator


def test_red_busy_alias_coordinators_converge_without_overlap() -> None:
    async def scenario() -> BusyMergeResult:
        registry = DeliveryCoordinatorRegistry()
        alias, canonical = "old:FriendMessage:42", "new:FriendMessage:42"
        for session_id in (alias, alias, canonical):
            registry.record_activity(session_id)
        both_entered, release, post_entered = (anyio.Event() for _index in range(3))
        active = post_overlap = 0
        merged = None
        error = None

        async def owner(session_id: str) -> None:
            nonlocal active
            async with registry.coordinator_for(session_id).lease():
                active += 1
                if active == 2:
                    both_entered.set()
                await release.wait()
                active -= 1

        async def post_merge() -> None:
            nonlocal post_overlap
            async with registry.coordinator_for(alias).lease():
                post_overlap = active
                post_entered.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(owner, alias)
            task_group.start_soon(owner, canonical)
            await both_entered.wait()
            try:
                merged = registry.merge_aliases(alias, canonical)
            except RuntimeError as caught:
                error = caught
            if error is None:
                task_group.start_soon(post_merge)
            release.set()
            if error is None:
                await post_entered.wait()
        revision = merged.revision if merged else -1
        return error, revision, registry.coordinator_count, post_overlap

    assert anyio.run(scenario) == (None, 2, 1, 0), (
        "PCF-02 RED: busy alias convergence must retain revision and serialize future leases"
    )


def test_red_observable_barrier_orders_snapshot_before_state_merge(
    main_barrier_runner: MainBarrierRunner,
) -> None:
    result = main_barrier_runner(False)
    assert result[:3] == (True, False, True), (
        "PCF-02 RED: gate snapshot must observably precede awaited state merge and lease"
    )


def test_red_activity_during_prelease_state_merge_invalidates_run(
    main_barrier_runner: MainBarrierRunner,
) -> None:
    result = main_barrier_runner(True)
    assert result[3] == 0, (
        "PCF-02 RED: activity during awaited pre-lease state merge must invalidate the started run"
    )


@pytest.mark.parametrize("boundary", ("provider", "tts", "hook", "pre_finalization"))
@pytest.mark.parametrize("transition", ("activity", "disabled", "quiet"))
def test_red_transition_matrix_uses_observable_barriers(
    boundary_runner: BoundaryRunner,
    boundary: Boundary,
    transition: Literal["activity", "disabled", "quiet"],
) -> None:
    expected = (
        (1, 1, int(transition == "quiet"))
        if boundary == "pre_finalization"
        else (0, 0, 0)
    )
    assert boundary_runner(boundary, transition) == expected, (
        f"PCF-02 RED: {boundary}/{transition} transition violated the accepted-send-finalize matrix"
    )


@pytest.mark.parametrize("tts_outcome", ("false", "exception"))
def test_red_tts_failure_then_text_acceptance_is_partial(
    monkeypatch: pytest.MonkeyPatch, tts_outcome: Literal["false", "exception"]
) -> None:
    settings = {
        "tts_settings": {"enable_tts": True, "always_send_text": True},
        "segmented_reply_settings": {"enable": False},
    }
    monkeypatch.setattr(send, "get_session_config", lambda *_args: settings)

    class Provider:
        async def get_audio(self, _text: str) -> str:
            return "voice.wav"

    monkeypatch.setattr(send, "get_tts_provider", lambda *_args: Provider())

    async def tts_send(*_args, **_kwargs) -> bool:
        if tts_outcome == "exception":
            raise OSError
        return False

    async def text_send(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(send, "send_chain_with_hooks", text_send)

    turn = anyio.run(
        partial(
            send.dispatch_proactive_message,
            session_id="platform:FriendMessage:42",
            text="reply",
            config=SimpleNamespace(),
            context=SimpleNamespace(send_message=tts_send),
            session_data={},
        )
    )
    assert (turn.status, turn.intended_components, len(turn.accepted_components)) == (
        DispatchStatus.PARTIAL,
        2,
        1,
    ), (
        "PCF-02 RED: failed intended TTS plus accepted text must be partial with 1/2 accounting"
    )


def test_activity_during_history_delay_skips_write(
    history_runner: HistoryRunner,
) -> None:
    assert history_runner("delay") == (False, 0, False)


def test_red_activity_during_awaited_history_write_prevents_commit(
    history_runner: HistoryRunner,
) -> None:
    saved, writes, watch_registered = history_runner("write")
    assert watch_registered and not saved and writes == 0, (
        "PCF-02 RED: activity during awaited history write must cancel before stale commit"
    )


def test_real_sqlite_activity_during_commit_await_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def scenario() -> tuple[bool, int, int]:
        session_id = "platform:FriendMessage:42"
        conversation_id = "00000000-0000-0000-0000-000000000042"
        monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
        database = SQLiteDatabase(str(tmp_path / "astrbot.db"))
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(session_id)
        manager = ConversationManager(database)
        entered = anyio.Event()
        never_release = anyio.Event()
        commit_calls = 0
        barrier_hits = 0

        await database.create_conversation(
            user_id=session_id,
            platform_id="platform",
            cid=conversation_id,
        )
        real_commit = Connection.commit

        async def barrier_commit(connection: Connection) -> None:
            nonlocal barrier_hits, commit_calls
            commit_calls += 1
            if commit_calls == 1:
                barrier_hits += 1
                entered.set()
                await never_release.wait()
            await real_commit(connection)

        monkeypatch.setattr(Connection, "commit", barrier_commit)
        plugin = SimpleNamespace(
            config={},
            _delivery_coordinators=registry,
            _history_save_lock=anyio.Lock(),
            context=SimpleNamespace(conversation_manager=manager),
            _gate_verdict=lambda current_gate: registry.verdict(
                current_gate, enabled=True, quiet_hours=False
            ),
        )
        saved = False

        async def save_history() -> None:
            nonlocal saved
            saved = await chat_executor._save_conversation_history(
                plugin,
                {
                    "history_settings": {
                        "save_proactive_history": True,
                        "history_save_delay_seconds": 0,
                        "history_save_retry_attempts": 0,
                    }
                },
                conversation_id,
                "prompt",
                "response",
                gate,
            )

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(save_history)
                await entered.wait()
                registry.record_activity(session_id)
            conversation = await database.get_conversation_by_id(conversation_id)
            committed_parts = len(conversation.content or [])
            return saved, committed_parts, barrier_hits
        finally:
            await database.engine.dispose()

    saved, committed_parts, barrier_hits = anyio.run(scenario)
    assert not saved and committed_parts == 0 and barrier_hits == 1, (
        "PCF-02 RED: cancellation during the driver commit await must roll back "
        f"(saved={int(saved)} committed_parts={committed_parts} "
        f"barrier_hits={barrier_hits})"
    )


def test_real_sqlite_postcommit_compensation_preserves_concurrent_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def scenario() -> tuple[bool, list[tuple[str, str]], int]:
        session_id = "platform:FriendMessage:42"
        conversation_id = "00000000-0000-0000-0000-000000000043"
        monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
        database = SQLiteDatabase(str(tmp_path / "astrbot-concurrent.db"))
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(session_id)
        manager = ConversationManager(database)
        entered = anyio.Event()
        never_release = anyio.Event()
        get_calls = 0

        await database.create_conversation(
            user_id=session_id,
            platform_id="platform",
            cid=conversation_id,
        )
        real_get = database.get_conversation_by_id

        async def barrier_get(cid: str):
            nonlocal get_calls
            conversation = await real_get(cid)
            get_calls += 1
            if get_calls == 2:
                entered.set()
                await never_release.wait()
            return conversation

        monkeypatch.setattr(database, "get_conversation_by_id", barrier_get)
        plugin = SimpleNamespace(
            config={},
            _delivery_coordinators=registry,
            _history_save_lock=anyio.Lock(),
            context=SimpleNamespace(conversation_manager=manager),
            _gate_verdict=lambda current_gate: registry.verdict(
                current_gate, enabled=True, quiet_hours=False
            ),
        )
        saved = False

        async def save_history() -> None:
            nonlocal saved
            saved = await chat_executor._save_conversation_history(
                plugin,
                {
                    "history_settings": {
                        "save_proactive_history": True,
                        "history_save_delay_seconds": 0,
                        "history_save_retry_attempts": 0,
                    }
                },
                conversation_id,
                "proactive prompt",
                "proactive response",
                gate,
            )

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(save_history)
                await entered.wait()
                await manager.add_message_pair(
                    cid=conversation_id,
                    user_message={"role": "user", "content": "legitimate prompt"},
                    assistant_message={
                        "role": "assistant",
                        "content": "legitimate response",
                    },
                )
                registry.record_activity(session_id)
            conversation = await real_get(conversation_id)
            content = conversation.content or []
            messages = [(message["role"], message["content"]) for message in content]
            return saved, messages, get_calls
        finally:
            await database.engine.dispose()

    saved, messages, get_calls = anyio.run(scenario)
    assert not saved and messages == [
        ("user", "legitimate prompt"),
        ("assistant", "legitimate response"),
    ], (
        "PCF-02 RED: postcommit compensation must remove only the proactive pair "
        f"(saved={int(saved)} messages={messages!r} get_calls={get_calls})"
    )


def test_real_sqlite_external_cancellation_and_activity_cleanup_before_propagation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def scenario() -> tuple[bool, int]:
        session_id = "platform:FriendMessage:42"
        conversation_id = "00000000-0000-0000-0000-000000000044"
        monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
        database = SQLiteDatabase(str(tmp_path / "astrbot-cancel.db"))
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(session_id)
        manager = ConversationManager(database)
        entered = anyio.Event()
        never_release = anyio.Event()
        get_calls = 0

        await database.create_conversation(
            user_id=session_id,
            platform_id="platform",
            cid=conversation_id,
        )
        real_get = database.get_conversation_by_id

        async def barrier_get(cid: str):
            nonlocal get_calls
            conversation = await real_get(cid)
            get_calls += 1
            if get_calls == 2:
                entered.set()
                await never_release.wait()
            return conversation

        monkeypatch.setattr(database, "get_conversation_by_id", barrier_get)
        plugin = SimpleNamespace(
            config={},
            _delivery_coordinators=registry,
            _history_save_lock=anyio.Lock(),
            context=SimpleNamespace(conversation_manager=manager),
            _gate_verdict=lambda current_gate: registry.verdict(
                current_gate, enabled=True, quiet_hours=False
            ),
        )

        async def cancel_after_commit(scope: anyio.CancelScope) -> None:
            await entered.wait()
            registry.record_activity(session_id)
            scope.cancel()

        try:
            with anyio.CancelScope() as external_scope:
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(cancel_after_commit, external_scope)
                    await chat_executor._save_conversation_history(
                        plugin,
                        {
                            "history_settings": {
                                "save_proactive_history": True,
                                "history_save_delay_seconds": 0,
                                "history_save_retry_attempts": 0,
                            }
                        },
                        conversation_id,
                        "prompt",
                        "response",
                        gate,
                    )
            conversation = await real_get(conversation_id)
            return external_scope.cancelled_caught, len(conversation.content or [])
        finally:
            await database.engine.dispose()

    cancellation_propagated, committed_parts = anyio.run(scenario)
    assert cancellation_propagated and committed_parts == 0, (
        "PCF-02 RED: cancellation must propagate only after stale history cleanup "
        f"(propagated={int(cancellation_propagated)} "
        f"committed_parts={committed_parts})"
    )


def test_real_sqlite_success_uses_opaque_identity_and_strips_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def scenario() -> tuple[bool, list[dict], list[dict]]:
        session_id = "platform:FriendMessage:42"
        conversation_id = "00000000-0000-0000-0000-000000000045"
        monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
        database = SQLiteDatabase(str(tmp_path / "astrbot-marker.db"))
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(session_id)
        manager = ConversationManager(database)
        captured: list[dict] = []

        await database.create_conversation(
            user_id=session_id,
            platform_id="platform",
            cid=conversation_id,
        )
        real_add = manager.add_message_pair

        async def capture_pair(**kwargs) -> None:
            captured.extend((kwargs["user_message"], kwargs["assistant_message"]))
            await real_add(**kwargs)

        monkeypatch.setattr(manager, "add_message_pair", capture_pair)
        plugin = SimpleNamespace(
            config={},
            _delivery_coordinators=registry,
            _history_save_lock=anyio.Lock(),
            context=SimpleNamespace(conversation_manager=manager),
            _gate_verdict=lambda current_gate: registry.verdict(
                current_gate, enabled=True, quiet_hours=False
            ),
        )

        try:
            saved = await chat_executor._save_conversation_history(
                plugin,
                {
                    "history_settings": {
                        "save_proactive_history": True,
                        "history_save_delay_seconds": 0,
                        "history_save_retry_attempts": 0,
                    }
                },
                conversation_id,
                "original prompt",
                "assistant response",
                gate,
            )
            conversation = await database.get_conversation_by_id(conversation_id)
            return saved, captured, conversation.content or []
        finally:
            await database.engine.dispose()

    saved, captured, persisted = anyio.run(scenario)
    marker_key = "_astrbot_proactive_history_entry_id"
    assert (
        saved
        and len(captured) == 2
        and all(
            isinstance(message, dict) and message.get(marker_key)
            for message in captured
        )
    ), "PCF-02 RED: the public history call must receive opaque identity metadata"
    assert captured[0][marker_key] == captured[1][marker_key]
    parsed_user = UserMessageSegment.model_validate(captured[0])
    parsed_assistant = AssistantMessageSegment.model_validate(captured[1])
    assert parsed_user.content == [TextPart(text="original prompt")]
    assert parsed_assistant.content == [TextPart(text="assistant response")]
    assert marker_key not in parsed_assistant.model_dump()
    assert all(marker_key not in message for message in persisted), (
        "PCF-02 RED: successful history must not retain private marker metadata"
    )


def test_real_sqlite_inbound_handler_waits_for_compensation_before_core_append(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def scenario() -> tuple[bool, bool, list[tuple[str, str]]]:
        session_id = "platform:FriendMessage:42"
        conversation_id = "00000000-0000-0000-0000-000000000046"
        monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
        database = SQLiteDatabase(str(tmp_path / "astrbot-inbound.db"))
        registry = DeliveryCoordinatorRegistry()
        gate = registry.record_activity(session_id)
        coordinator = registry.coordinator_for(session_id)
        manager = ConversationManager(database)
        entered = anyio.Event()
        never_release = anyio.Event()
        get_calls = 0
        compensation_done = False
        handler_completed_before_compensation = False

        await database.create_conversation(
            user_id=session_id,
            platform_id="platform",
            cid=conversation_id,
        )
        real_get = database.get_conversation_by_id

        async def barrier_get(cid: str):
            nonlocal get_calls
            conversation = await real_get(cid)
            get_calls += 1
            if get_calls == 2:
                entered.set()
                await never_release.wait()
            return conversation

        monkeypatch.setattr(database, "get_conversation_by_id", barrier_get)
        plugin = SimpleNamespace(
            config={},
            _delivery_coordinators=registry,
            _history_save_lock=anyio.Lock(),
            context=SimpleNamespace(conversation_manager=manager),
            _gate_verdict=lambda current_gate: registry.verdict(
                current_gate, enabled=True, quiet_hours=False
            ),
            _canonical_delivery_session=lambda alias: alias,
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
        saved = False

        async def proactive_history() -> None:
            nonlocal compensation_done, saved
            async with coordinator.lease():
                saved = await chat_executor._save_conversation_history(
                    plugin,
                    {
                        "history_settings": {
                            "save_proactive_history": True,
                            "history_save_delay_seconds": 0,
                            "history_save_retry_attempts": 0,
                        }
                    },
                    conversation_id,
                    "proactive prompt",
                    "proactive response",
                    gate,
                )
                compensation_done = True

        async def inbound_pipeline() -> None:
            nonlocal handler_completed_before_compensation
            await entered.wait()
            await ProactiveChatPlugin._handle_message(plugin, event, is_group=False)
            handler_completed_before_compensation = not compensation_done
            await manager.add_message_pair(
                cid=conversation_id,
                user_message={"role": "user", "content": "incoming prompt"},
                assistant_message={"role": "assistant", "content": "incoming response"},
            )

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(proactive_history)
                task_group.start_soon(inbound_pipeline)
            conversation = await real_get(conversation_id)
            content = conversation.content or []
            messages = [(message["role"], message["content"]) for message in content]
            return saved, handler_completed_before_compensation, messages
        finally:
            await database.engine.dispose()

    saved, handler_was_early, messages = anyio.run(scenario)
    assert (
        not saved
        and not handler_was_early
        and messages
        == [
            ("user", "incoming prompt"),
            ("assistant", "incoming response"),
        ]
    ), (
        "PCF-02 RED: inbound core history must append only after compensation "
        f"(saved={int(saved)} handler_was_early={int(handler_was_early)} "
        f"messages={messages!r})"
    )


def test_finalization_cas_prevents_stale_mutation() -> None:
    async def scenario() -> tuple[bool, int, int]:
        registry = DeliveryCoordinatorRegistry()
        session_id = "platform:GroupMessage:42"
        gate = registry.record_activity(session_id)
        registry.record_activity(session_id)
        calls = [0, 0]

        async def save() -> None:
            calls[0] += 1

        def schedule(*_args, **_kwargs) -> None:
            calls[1] += 1

        plugin = SimpleNamespace(
            _find_habit_task=lambda *_args: None,
            data_lock=anyio.Lock(),
            session_data={session_id: {"unanswered_count": 0}},
            _save_data=save,
            _add_scheduled_job_at=schedule,
            timezone=None,
            _gate_verdict=lambda current: registry.verdict(
                current, enabled=True, quiet_hours=False
            ),
        )
        changed = await chat_executor._update_unanswered_and_reschedule(
            plugin, session_id, {}, 0, gate=gate, clear_task_description=True
        )
        return changed, *calls

    assert anyio.run(scenario) == (False, 0, 0)
