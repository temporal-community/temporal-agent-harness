# ABOUTME: The search attributes a swarm member publishes about itself, so ONE visibility query
# (``SwarmId = '<id>'``) lists a whole swarm — every member's role, parent (Temporal's built-in
# ``ParentWorkflowId``) and live phase — without describing or querying any of them.
#
# The namespace must register these before a swarm runs: Temporal fails the workflow task of an
# agent that upserts an unregistered attribute. The root ``just temporal`` recipe starts the dev
# server with them (``dev_server_flags()`` lists the same flags); in tests, pass
# ``SWARM_SEARCH_ATTRIBUTES`` to ``WorkflowEnvironment.start_local``; on any other namespace, add
# them once (``temporal operator search-attribute create``, or ``tcld`` on Temporal Cloud).

from __future__ import annotations

from temporalio.common import (
    SearchAttributeKey,
    SearchAttributePair,
    SearchAttributeUpdate,
)

from temporal_agent_harness.harness.agent_protocol import AgentPhase, SwarmMembership

SWARM_ID = SearchAttributeKey.for_keyword("SwarmId")
"""The swarm the workflow belongs to (:attr:`SwarmMembership.swarm_id`)."""

AGENT_ROLE = SearchAttributeKey.for_keyword("AgentRole")
"""The member's role in its swarm (:attr:`SwarmMembership.role`); the board's is ``board``."""

AGENT_PHASE = SearchAttributeKey.for_keyword("AgentPhase")
"""The member's live :class:`AgentPhase`, upserted whenever it changes."""

AGENT_LAST_ACTIVE = SearchAttributeKey.for_datetime("AgentLastActive")
"""When the member's phase last changed — so a console can sort by staleness."""

AGENT_PEERS = SearchAttributeKey.for_keyword_list("AgentPeers")
"""The workflow ids of the peers the member has a turn in flight to, right now — the live peer
edges of a swarm view, readable from visibility without reading anyone's stream."""

SWARM_SEARCH_ATTRIBUTES: tuple[SearchAttributeKey, ...] = (
    SWARM_ID,
    AGENT_ROLE,
    AGENT_PHASE,
    AGENT_LAST_ACTIVE,
    AGENT_PEERS,
)
"""Every swarm search attribute, for registering them on a namespace."""

BOARD_ROLE = "board"
"""The ``AgentRole`` the swarm board workflow carries."""


def membership_pairs(membership: SwarmMembership) -> list[SearchAttributePair]:
    """The ``SwarmId`` / ``AgentRole`` pairs to start a member workflow with.

    Setting them at start (rather than only upserting them from inside) makes a new member
    visible to a swarm query before its first workflow task runs."""
    pairs: list[SearchAttributePair] = [SearchAttributePair(SWARM_ID, membership.swarm_id)]
    if membership.role is not None:
        pairs.append(SearchAttributePair(AGENT_ROLE, membership.role))
    return pairs


def membership_updates(membership: SwarmMembership) -> list[SearchAttributeUpdate]:
    """The ``SwarmId`` / ``AgentRole`` upserts matching :func:`membership_pairs`."""
    updates: list[SearchAttributeUpdate] = [SWARM_ID.value_set(membership.swarm_id)]
    if membership.role is not None:
        updates.append(AGENT_ROLE.value_set(membership.role))
    return updates


def dev_server_flags() -> list[str]:
    """``temporal server start-dev`` flags that register every swarm search attribute."""
    kinds = {
        "KEYWORD": "Keyword",
        "DATETIME": "Datetime",
        "KEYWORD_LIST": "KeywordList",
    }
    flags: list[str] = []
    for key in SWARM_SEARCH_ATTRIBUTES:
        flags += ["--search-attribute", f"{key.name}={kinds[key.indexed_value_type.name]}"]
    return flags


__all__ = [
    "AGENT_LAST_ACTIVE",
    "AGENT_PEERS",
    "AGENT_PHASE",
    "AGENT_ROLE",
    "AgentPhase",
    "BOARD_ROLE",
    "SWARM_ID",
    "SWARM_SEARCH_ATTRIBUTES",
    "dev_server_flags",
    "membership_pairs",
    "membership_updates",
]

