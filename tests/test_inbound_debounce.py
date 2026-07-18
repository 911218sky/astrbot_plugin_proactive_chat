from __future__ import annotations

from types import SimpleNamespace

import anyio

from astrbot_plugin_proactive_chat import main


def test_human_like_settings_expose_inbound_message_wait() -> None:
    settings = main.resolve_human_like_settings(
        {
            "human_like_settings": {
                "enable": True,
                "inbound_debounce_seconds": 6,
            }
        }
    )
    assert settings.inbound_debounce_seconds == 6


def test_inbound_debounce_only_latest_event_continues() -> None:
    async def scenario() -> None:
        plugin = SimpleNamespace(_inbound_debounce_tokens={})
        first_started = anyio.Event()
        release_first = anyio.Event()
        second_started = anyio.Event()
        release_second = anyio.Event()

        async def first_sleep(_delay: float) -> None:
            first_started.set()
            await release_first.wait()

        async def second_sleep(_delay: float) -> None:
            second_started.set()
            await release_second.wait()

        first_result: list[bool] = []
        second_result: list[bool] = []

        async def run_first() -> None:
            first_result.append(
                await main.ProactiveChatPlugin._wait_for_inbound_quiet(
                    plugin,
                    "platform:FriendMessage:42",
                    {"human_like_settings": {"enable": True, "inbound_debounce_seconds": 3}},
                    sleep=first_sleep,
                )
            )

        async def run_second() -> None:
            await first_started.wait()
            second_result.append(
                await main.ProactiveChatPlugin._wait_for_inbound_quiet(
                    plugin,
                    "platform:FriendMessage:42",
                    {"human_like_settings": {"enable": True, "inbound_debounce_seconds": 3}},
                    sleep=second_sleep,
                )
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_first)
            task_group.start_soon(run_second)
            await second_started.wait()
            release_first.set()
            await anyio.sleep(0)
            release_second.set()

        assert first_result == [False]
        assert second_result == [True]
        assert plugin._inbound_debounce_tokens == {}

    anyio.run(scenario)


def test_stale_inbound_event_stops_pipeline_before_llm(
    monkeypatch,
) -> None:
    class Event:
        unified_msg_origin = "platform:FriendMessage:42"

        def __init__(self) -> None:
            self.stopped = False

        def get_messages(self) -> list[str]:
            return ["first segment"]

        def stop_event(self) -> None:
            self.stopped = True

    async def scenario() -> None:
        event = Event()
        monkeypatch.setattr(
            main,
            "get_session_config",
            lambda *_args: {"enable": True},
        )
        plugin = SimpleNamespace(
            config={},
            _canonical_delivery_session=lambda session_id: session_id,
            _wait_for_inbound_quiet=lambda *_args, **_kwargs: _false_async(),
        )

        await main.ProactiveChatPlugin._handle_message(
            plugin,
            event,
            is_group=False,
        )

        assert event.stopped is True

    async def _false_async() -> bool:
        return False

    anyio.run(scenario)


def test_inbound_wait_uses_human_reply_timing() -> None:
    async def scenario() -> None:
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        plugin = SimpleNamespace(_inbound_debounce_tokens={})
        result = await main.ProactiveChatPlugin._wait_for_inbound_quiet(
            plugin,
            "platform:FriendMessage:42",
            {
                "human_like_settings": {
                    "enable": True,
                    "timing_min_seconds": 20,
                    "timing_max_seconds": 32,
                    "inbound_debounce_seconds": 3,
                }
            },
            message_text="hello",
            local_hour=12,
            random_value=0.0,
            sleep=record_sleep,
        )

        assert result is True
        assert delays == [20]

    anyio.run(scenario)
