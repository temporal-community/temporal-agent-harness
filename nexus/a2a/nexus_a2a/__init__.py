"""Harness-independent A2A protocol binding for Temporal Nexus."""

from .authoring import (
    A2ABackend,
    NexusA2AServiceHandler,
    OperationContext,
    make_agent_card,
)
from .client import DEFAULT_POLL_TIMEOUT_SECONDS, NexusA2AClient
from .context import RequestContext
from .converter import (
    A2AJSONPlainPayloadConverter,
    A2ANexusPayloadConverter,
    a2a_nexus_data_converter,
    a2a_payload_converters,
)
from .errors import (
    A2ABackendError,
    BackendErrorKind,
    NexusA2AError,
    NexusA2AOperationError,
)
from .execution import NexusA2AExecutor, StandaloneNexusExecutor, WorkflowNexusExecutor
from .frontends import (
    NexusA2AClientTransport,
    create_nexus_a2a_client,
    register_nexus_a2a_transport,
)
from .service import (
    A2A_NEXUS_BINDING,
    A2A_PROTOCOL_VERSION,
    A2A_SERVICE_NAME,
    A2AService,
    SubscribeToTaskInput,
    SubscribeToTaskItem,
    SubscribeToTaskOutput,
)
from .stream import StreamRecord, decode_stream_item, decode_stream_record

__all__ = [
    "A2A_NEXUS_BINDING",
    "A2A_PROTOCOL_VERSION",
    "A2A_SERVICE_NAME",
    "DEFAULT_POLL_TIMEOUT_SECONDS",
    "A2ABackend",
    "A2ABackendError",
    "A2AJSONPlainPayloadConverter",
    "A2ANexusPayloadConverter",
    "A2AService",
    "BackendErrorKind",
    "NexusA2AClient",
    "NexusA2AClientTransport",
    "NexusA2AError",
    "NexusA2AExecutor",
    "NexusA2AOperationError",
    "NexusA2AServiceHandler",
    "OperationContext",
    "RequestContext",
    "StandaloneNexusExecutor",
    "StreamRecord",
    "SubscribeToTaskInput",
    "SubscribeToTaskItem",
    "SubscribeToTaskOutput",
    "WorkflowNexusExecutor",
    "a2a_nexus_data_converter",
    "a2a_payload_converters",
    "create_nexus_a2a_client",
    "decode_stream_item",
    "decode_stream_record",
    "make_agent_card",
    "register_nexus_a2a_transport",
]
