"""A registry store behind a Nexus service, which a deployment implements in any Temporal SDK.

The service, ``AgentKitRegistryStore`` (``contract/registry-store.nexusrpc.yaml``), holds the
registry document under a version and has two synchronous operations:

- ``read`` takes ``{}`` and returns ``{"snapshot": {...}, "version": n}``: the stored registry,
  or ``{"snapshot": {"agents": {}}, "version": 0}`` when nothing has been written.
- ``write`` takes ``{"snapshot": {...}, "expected": n}``. When the stored version is ``n`` it
  stores the snapshot as version ``n + 1`` and returns ``{"stored": true, "version": n + 1}``;
  otherwise it stores nothing and returns ``{"stored": false, "version": <the stored version>}``.

The registry's rules run in the server, so the service only compares versions. The server calls
it as standalone Nexus operations with its own Temporal client, so a store needs no credentials of
its own: the endpoint's access rules decide who may call it. The bodies' JSON Schema is
``contract/registry-store.schema.json`` (``temporal-agent-kit schema --registry-store``).

Standalone Nexus operations are experimental in Temporal and in its Python SDK.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from typing import Any

import nexusrpc
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema
from temporalio.client import Client, NexusOperationFailureError
from temporalio.service import RPCError

from temporal_agent_kit.web.registry_store import (
    RegistrySnapshot,
    RegistryWrite,
    StaleRegistry,
    VersionedSnapshot,
)

REGISTRY_STORE_SERVICE = "AgentKitRegistryStore"
_OPERATION_TIMEOUT = timedelta(seconds=30)


class RegistryRead(BaseModel):
    """A ``read``'s input, which carries nothing."""


class RegistryWriteOutcome(BaseModel):
    """A ``write``'s output: whether the snapshot was stored, and the version now stored."""

    stored: bool
    version: int


@nexusrpc.service(name=REGISTRY_STORE_SERVICE)
class RegistryStoreService:
    """The registry store contract, for a handler to implement and the server to call."""

    read: nexusrpc.Operation[RegistryRead, VersionedSnapshot]
    write: nexusrpc.Operation[RegistryWrite, RegistryWriteOutcome]


class RegistryStoreError(Exception):
    """The registry store's endpoint could not be reached, or its operation failed."""


class NexusRegistryStore:
    """An :class:`~temporal_agent_kit.web.registry_store.AgentRegistryStore` over the
    ``AgentKitRegistryStore`` service at a Nexus ``endpoint``."""

    def __init__(self, client: Client, endpoint: str) -> None:
        self.endpoint = endpoint
        self._nexus = client.create_nexus_client(RegistryStoreService, endpoint)

    async def read(self) -> VersionedSnapshot:
        return await self._execute(RegistryStoreService.read, RegistryRead(), "read")

    async def write(self, snapshot: RegistrySnapshot, expected: int) -> int:
        outcome = await self._execute(
            RegistryStoreService.write, RegistryWrite(snapshot=snapshot, expected=expected), "write"
        )
        if not outcome.stored:
            raise StaleRegistry(
                f"The registry at {self.endpoint} is at version {outcome.version}, not {expected}."
            )
        return outcome.version

    async def _execute(self, operation: Any, arg: BaseModel, name: str) -> Any:
        try:
            return await self._nexus.execute_operation(
                operation,
                arg,
                id=f"agent-kit-registry-{name}-{uuid.uuid4()}",
                schedule_to_close_timeout=_OPERATION_TIMEOUT,
            )
        except (NexusOperationFailureError, RPCError) as e:
            cause = e.cause if isinstance(e, NexusOperationFailureError) else e
            raise RegistryStoreError(
                f"The registry store's {name} on Nexus endpoint {self.endpoint!r} failed: {cause}"
            ) from e


def nexus_registry_store(
    endpoint: str,
) -> Callable[[Client], AbstractAsyncContextManager[NexusRegistryStore]]:
    """A :data:`~temporal_agent_kit.web.registry_store.RegistryStoreFactory` for the
    ``AgentKitRegistryStore`` service at the Nexus ``endpoint``. Entering it reads the registry
    once, so a store that cannot be reached stops the server as it starts rather than at its first
    request."""

    @asynccontextmanager
    async def factory(client: Client) -> AsyncIterator[NexusRegistryStore]:
        store = NexusRegistryStore(client, endpoint)
        await store.read()
        yield store

    return factory


def registry_store_schema() -> dict[str, Any]:
    """The JSON Schema of the ``AgentKitRegistryStore`` operations' inputs and outputs, which a
    store in any Temporal SDK implements::

        {"read": {"input": ..., "output": ...},
         "write": {"input": ..., "output": ...},
         "$defs": {...}}
    """
    from temporal_agent_kit.agent_schema import _JSON_VALUE_SCHEMA, _REF_TEMPLATE, _WireJsonSchema

    models = [RegistryRead, VersionedSnapshot, RegistryWrite, RegistryWriteOutcome]
    refs, top = models_json_schema(
        [(model, "serialization") for model in models],
        ref_template=_REF_TEMPLATE,
        schema_generator=_WireJsonSchema,
    )
    defs: dict[str, Any] = top.get("$defs", {})
    if "JsonValue" in defs:
        defs["JsonValue"] = _JSON_VALUE_SCHEMA

    def ref(model: type[BaseModel]) -> Any:
        return refs[(model, "serialization")]

    return {
        "read": {"input": ref(RegistryRead), "output": ref(VersionedSnapshot)},
        "write": {"input": ref(RegistryWrite), "output": ref(RegistryWriteOutcome)},
        "$defs": dict(sorted(defs.items())),
    }


def dump_registry_store_schema() -> str:
    """:func:`registry_store_schema` as stable, diffable JSON text."""
    return json.dumps(registry_store_schema(), indent=2, ensure_ascii=False) + "\n"
