"""The registry's rules, which every registry store shares, and the read-change-write loop that
applies them to a store."""

from __future__ import annotations

import pytest

from temporal_agent_kit.web.memory_registry_store import InMemoryRegistryStore
from temporal_agent_kit.web.registry_changes import (
    CHANGE_ATTEMPTS,
    RegistryBusy,
    RegistryConflict,
    UnknownAgent,
    change_registry,
    deleted,
    registered,
    retired,
)
from temporal_agent_kit.web.registry_store import (
    RegisteredAgent,
    RegistrySnapshot,
    StaleRegistry,
    VersionedSnapshot,
)


def _agent(key: str, workflow_type: str | None = None, **fields: object) -> RegisteredAgent:
    return RegisteredAgent.model_validate(
        {
            "key": key,
            "workflow_type": workflow_type or f"{key.title()}Agent",
            "task_queue": f"{key}-queue",
            "label": key.title(),
            "description": f"The {key} agent.",
            **fields,
        }
    )


def test_registering_adds_every_agent() -> None:
    snapshot = registered(RegistrySnapshot(), [_agent("a"), _agent("b")])
    assert sorted(snapshot.agents) == ["a", "b"]


def test_registering_a_key_again_replaces_it_and_makes_it_active() -> None:
    before = RegistrySnapshot.of([_agent("a", retired=True)])

    after = registered(before, [_agent("a", label="Renamed")])

    assert after.agents["a"].label == "Renamed"
    assert after.agents["a"].retired is False


@pytest.mark.parametrize(
    ("existing", "agents", "message"),
    [
        pytest.param([], [_agent("a"), _agent("a")], "given twice", id="a key twice"),
        pytest.param(
            [], [_agent("a", "Same"), _agent("b", "Same")], "given for both", id="a type twice"
        ),
        pytest.param(
            [_agent("a", "Same")], [_agent("b", "Same")], "already registered", id="a taken type"
        ),
        pytest.param(
            [_agent("a")], [_agent("a", "OtherAgent")], "Delete it", id="a repointed type"
        ),
        pytest.param(
            [_agent("a")],
            [_agent("a").model_copy(update={"task_queue": "elsewhere"})],
            "Delete it",
            id="a repointed queue",
        ),
    ],
)
def test_registering_refuses_what_would_break_routing(
    existing: list[RegisteredAgent], agents: list[RegisteredAgent], message: str
) -> None:
    with pytest.raises(RegistryConflict, match=message):
        registered(RegistrySnapshot.of(existing), agents)


def test_registering_a_key_again_beside_new_agents_keeps_its_own_type() -> None:
    snapshot = RegistrySnapshot.of([_agent("a", "Same")])
    assert registered(snapshot, [_agent("a", "Same"), _agent("b")]).agents.keys() == {"a", "b"}


def test_retiring_keeps_the_agent_and_deleting_removes_it() -> None:
    snapshot = RegistrySnapshot.of([_agent("a"), _agent("b")])

    assert retired(snapshot, "a").agents["a"].retired is True
    assert sorted(deleted(snapshot, "a").agents) == ["b"]


@pytest.mark.parametrize("change", [retired, deleted])
def test_retiring_or_deleting_an_unknown_key_is_refused(change) -> None:
    with pytest.raises(UnknownAgent):
        change(RegistrySnapshot(), "nobody")


async def test_a_change_is_written_under_the_next_version() -> None:
    store = InMemoryRegistryStore()

    await change_registry(store, lambda s: registered(s, [_agent("a")]))

    stored = await store.read()
    assert stored.version == 1
    assert list(stored.snapshot.agents) == ["a"]


async def test_a_change_that_changes_nothing_is_not_written() -> None:
    store = InMemoryRegistryStore(RegistrySnapshot.of([_agent("a")]))

    await change_registry(store, lambda s: registered(s, [_agent("a")]))

    assert (await store.read()).version == 1


class _RacedStore(InMemoryRegistryStore):
    """A store another writer gets to first ``races`` times."""

    def __init__(self, races: int) -> None:
        super().__init__()
        self.races = races
        self.writes = 0

    async def write(self, snapshot: RegistrySnapshot, expected: int) -> int:
        self.writes += 1
        if self.races:
            self.races -= 1
            other = (await self.read()).snapshot
            await super().write(registered(other, [_agent(f"other{self.races}")]), expected)
            raise StaleRegistry("raced")
        return await super().write(snapshot, expected)


async def test_a_change_that_loses_a_race_is_reapplied_to_what_won() -> None:
    store = _RacedStore(races=2)

    await change_registry(store, lambda s: registered(s, [_agent("a")]))

    assert sorted((await store.read()).snapshot.agents) == ["a", "other0", "other1"]


async def test_a_change_that_always_loses_gives_up() -> None:
    store = _RacedStore(races=CHANGE_ATTEMPTS)

    with pytest.raises(RegistryBusy):
        await change_registry(store, lambda s: registered(s, [_agent("a")]))
    assert store.writes == CHANGE_ATTEMPTS


async def test_the_in_memory_store_refuses_a_stale_write() -> None:
    store = InMemoryRegistryStore()
    await store.write(RegistrySnapshot(), 0)

    with pytest.raises(StaleRegistry):
        await store.write(RegistrySnapshot(), 0)
    assert await store.read() == VersionedSnapshot(version=1)
