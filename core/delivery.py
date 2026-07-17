from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator

import anyio


class GateVerdict(str, Enum):
    CURRENT = "current"
    ACTIVITY_CHANGED = "activity_changed"
    DISABLED = "disabled"
    QUIET_HOURS = "quiet_hours"


class DispatchStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class AcceptedComponentKind(str, Enum):
    TTS = "tts"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class DispatchGate:
    canonical_session_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class AcceptedComponent:
    kind: AcceptedComponentKind
    content: str


@dataclass(frozen=True, slots=True)
class AcceptedTurn:
    message: str
    accepted_components: tuple[AcceptedComponent, ...]
    intended_components: int
    status: DispatchStatus
    verdict: GateVerdict = GateVerdict.CURRENT


def make_accepted_turn(
    message: str,
    accepted_components: tuple[AcceptedComponent, ...],
    *,
    intended_components: int,
    verdict: GateVerdict = GateVerdict.CURRENT,
) -> AcceptedTurn:
    accepted_count = len(accepted_components)
    if accepted_count == 0:
        status = DispatchStatus.FAILED
    elif accepted_count < intended_components:
        status = DispatchStatus.PARTIAL
    else:
        status = DispatchStatus.COMPLETE
    return AcceptedTurn(
        message=message,
        accepted_components=accepted_components,
        intended_components=intended_components,
        status=status,
        verdict=verdict,
    )


def accepted_turn_text(turn: AcceptedTurn) -> str:
    if any(
        component.kind is AcceptedComponentKind.TTS
        for component in turn.accepted_components
    ):
        return turn.message
    return "".join(
        component.content
        for component in turn.accepted_components
        if component.kind is AcceptedComponentKind.TEXT
    )


class _LockEntry:
    __slots__ = ("held", "lock", "order", "waiters")

    def __init__(self, order: int) -> None:
        self.order = order
        self.lock = anyio.Lock()
        self.held = False
        self.waiters = 0

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[None]:
        self.waiters += 1
        try:
            await self.lock.acquire()
        finally:
            self.waiters -= 1
        self.held = True
        try:
            yield
        finally:
            self.held = False
            self.lock.release()


class _CoordinatorGroup:
    __slots__ = ("entries", "retire_revision", "revision", "revision_signals")

    def __init__(
        self,
        entries: tuple[_LockEntry, ...],
        revision: int = 0,
        revision_signals: tuple[anyio.Event, ...] | None = None,
    ) -> None:
        self.entries = entries
        self.revision = revision
        self.revision_signals = revision_signals or (anyio.Event(),)
        self.retire_revision: int | None = None

    def record_activity(self) -> int:
        for signal in self.revision_signals:
            signal.set()
        self.revision += 1
        self.revision_signals = (anyio.Event(),)
        self.retire_revision = None
        return self.revision


class SessionCoordinator:
    __slots__ = ("_group",)

    def __init__(self, order: int) -> None:
        self._group = _CoordinatorGroup((_LockEntry(order),))

    @property
    def revision(self) -> int:
        return self._group.revision

    @property
    def locked(self) -> bool:
        return any(entry.held for entry in self._group.entries)

    @property
    def waiters(self) -> int:
        return sum(entry.waiters for entry in self._group.entries)

    @property
    def busy(self) -> bool:
        return self.locked or self.waiters > 0

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[None]:
        while True:
            group = self._group
            async with AsyncExitStack() as stack:
                for entry in group.entries:
                    await stack.enter_async_context(entry.lease())
                if self._group is not group:
                    continue
                yield
                return


class DeliveryCoordinatorRegistry:
    __slots__ = ("_aliases", "_coordinators", "_next_order")

    def __init__(self) -> None:
        self._aliases: dict[str, SessionCoordinator] = {}
        self._coordinators: list[SessionCoordinator] = []
        self._next_order = 0

    @property
    def coordinator_count(self) -> int:
        return len({id(coordinator) for coordinator in self._aliases.values()})

    def coordinator_for(self, session_id: str) -> SessionCoordinator:
        coordinator = self._aliases.get(session_id)
        if coordinator is None:
            coordinator = SessionCoordinator(self._next_order)
            self._next_order += 1
            self._coordinators.append(coordinator)
            self._aliases[session_id] = coordinator
        return coordinator

    def merge_aliases(
        self, alias_session_id: str, canonical_session_id: str
    ) -> SessionCoordinator:
        alias = self.coordinator_for(alias_session_id)
        canonical = self.coordinator_for(canonical_session_id)
        if alias is canonical:
            return canonical

        alias_group = alias._group
        canonical_group = canonical._group
        revision = max(alias_group.revision, canonical_group.revision)
        for group in (alias_group, canonical_group):
            if group.revision < revision:
                for signal in group.revision_signals:
                    signal.set()
        signals = tuple(
            signal
            for group in (alias_group, canonical_group)
            if group.revision == revision
            for signal in group.revision_signals
        )
        entries = tuple(
            sorted(
                (*alias_group.entries, *canonical_group.entries),
                key=lambda entry: entry.order,
            )
        )
        merged_group = _CoordinatorGroup(entries, revision, signals)
        survivor = canonical
        for coordinator in self._coordinators:
            if (
                coordinator._group is alias_group
                or coordinator._group is canonical_group
            ):
                coordinator._group = merged_group
        for known_alias, coordinator in tuple(self._aliases.items()):
            if coordinator._group is merged_group:
                self._aliases[known_alias] = survivor
        self._aliases[alias_session_id] = survivor
        self._aliases[canonical_session_id] = survivor
        return survivor

    def record_activity(
        self, alias_session_id: str, canonical_session_id: str | None = None
    ) -> DispatchGate:
        canonical_id = canonical_session_id or alias_session_id
        coordinator = self.merge_aliases(alias_session_id, canonical_id)
        revision = coordinator._group.record_activity()
        return DispatchGate(canonical_id, revision)

    def snapshot(
        self, alias_session_id: str, canonical_session_id: str | None = None
    ) -> DispatchGate:
        canonical_id = canonical_session_id or alias_session_id
        coordinator = self.merge_aliases(alias_session_id, canonical_id)
        return DispatchGate(canonical_id, coordinator.revision)

    def revision_signal(self, gate: DispatchGate) -> anyio.Event:
        coordinator = self._aliases.get(gate.canonical_session_id)
        if coordinator is not None and coordinator.revision == gate.revision:
            return coordinator._group.revision_signals[0]
        signal = anyio.Event()
        signal.set()
        return signal

    def retire(self, gate: DispatchGate) -> bool:
        coordinator = self._aliases.get(gate.canonical_session_id)
        if coordinator is None:
            return False

        group = coordinator._group
        gate_is_current = coordinator.revision == gate.revision
        if coordinator.busy:
            if gate_is_current:
                group.retire_revision = gate.revision
            return False
        if not gate_is_current and group.retire_revision != coordinator.revision:
            return False
        self._aliases = {
            alias: known
            for alias, known in self._aliases.items()
            if known._group is not group
        }
        self._coordinators = [
            known for known in self._coordinators if known._group is not group
        ]
        return True

    def verdict(
        self,
        gate: DispatchGate,
        *,
        enabled: bool,
        quiet_hours: bool,
    ) -> GateVerdict:
        coordinator = self._aliases.get(gate.canonical_session_id)
        if coordinator is None or coordinator.revision != gate.revision:
            return GateVerdict.ACTIVITY_CHANGED
        if not enabled:
            return GateVerdict.DISABLED
        if quiet_hours:
            return GateVerdict.QUIET_HOURS
        return GateVerdict.CURRENT
