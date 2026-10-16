"""Reading a mounted filesystem for the console: one page of a file, or an OKF bundle's graph.

A mount backed by an :class:`~temporal_agent_harness.harness.code_mode.ActivityFileSystem` has a
``FileIndex`` whose ``source`` names the filesystem, its config and the task queue its
activities run on. The viewer sends that source back with a path, and this runs the
filesystem's ``vfs.<name>.view`` activity as a standalone activity: no workflow is involved, so
viewing a file adds nothing to the agent's history and works after the agent has closed.

The same way, an OKF bundle mount's graph comes from the filesystem's ``vfs.<name>.okf_graph``
activity, which walks the bundle and returns its concepts and the links between them.

What comes back is the store as it is right now, not as it was at any earlier point in the
agent's history. Only ``view`` and ``okf_graph`` are ever called, never an operation that
changes a file; the config is the filesystem's to validate.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import PurePosixPath
from typing import TypeVar

from pydantic import BaseModel
from temporalio.client import ActivityFailureError, Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, TimeoutError
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.code_mode.activity_fs import (
    MAX_VIEW_BYTES,
    FileChunk,
    FsCall,
    FsRead,
    OKFGraphCall,
    activity_name,
)
from temporal_agent_harness.harness.code_mode.okf import OKFGraph
from temporal_agent_harness.harness.code_mode.vfs import IndexSource

T = TypeVar("T")

_FILESYSTEM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")

# Long enough for a worker to pick the call up and read a page; short enough that a missing
# worker fails the view instead of leaving it waiting.
_TIMEOUT = timedelta(seconds=15)
# A graph reads the whole bundle (within the walk's limits), so it gets longer.
_GRAPH_TIMEOUT = timedelta(seconds=60)

_STATUS_BY_ERROR = {
    "FileNotFoundError": 404,
    "IsADirectoryError": 400,
    "NotADirectoryError": 400,
    "PermissionError": 403,
    "ValueError": 400,
}


class FileViewRequest(BaseModel):
    """One page of a file: the mount's ``FileIndex.source``, the mount-relative path, and
    where the page starts."""

    source: IndexSource
    path: PurePosixPath
    offset: int = 0


class OKFGraphRequest(BaseModel):
    """An OKF bundle mount's graph: the mount's ``FileIndex.source``."""

    source: IndexSource


class FileViewError(Exception):
    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error


async def view_file(client: Client, request: FileViewRequest) -> FileChunk:
    """Read one page (at most ``MAX_VIEW_BYTES``) of ``request.path``."""
    return await _run(
        client,
        request.source,
        "view",
        FsRead(
            config=dict(request.source.config),
            path=request.path,
            offset=request.offset,
            length=MAX_VIEW_BYTES,
        ),
        FileChunk,
        _TIMEOUT,
    )


async def okf_graph(client: Client, request: OKFGraphRequest) -> OKFGraph:
    """The whole OKF bundle in the mount, with the links between its concepts."""
    return await _run(
        client,
        request.source,
        "okf_graph",
        OKFGraphCall(config=dict(request.source.config), path=PurePosixPath("."), links=True),
        OKFGraph,
        _GRAPH_TIMEOUT,
    )


async def _run(
    client: Client,
    source: IndexSource,
    operation: str,
    arg: FsCall,
    result_type: type[T],
    timeout: timedelta,
) -> T:
    """Run the filesystem's ``vfs.<name>.<operation>`` as a standalone activity on its task
    queue, turning its failures into :class:`FileViewError`\\ s."""
    if not _FILESYSTEM_NAME.fullmatch(source.filesystem):
        raise FileViewError(400, "invalid_filesystem", f"no filesystem {source.filesystem!r}")
    try:
        return await client.execute_activity(
            activity_name(source.filesystem, operation),
            arg,
            id=f"vfs-{operation.replace('_', '-')}-{uuid.uuid4()}",
            task_queue=source.task_queue,
            result_type=result_type,
            schedule_to_close_timeout=timeout,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
    except RPCError as e:
        if e.status == RPCStatusCode.UNIMPLEMENTED:
            raise FileViewError(
                501,
                "file_view_unavailable",
                "This Temporal server can't run standalone activities, so files can't be "
                "viewed. The local dev server supports them.",
            ) from e
        raise
    except ActivityFailureError as e:
        cause = e.cause
        if isinstance(cause, ApplicationError):
            status = _STATUS_BY_ERROR.get(cause.type or "", 502)
            raise FileViewError(status, cause.type or "view_failed", cause.message) from e
        if isinstance(cause, TimeoutError):
            raise FileViewError(
                504,
                "no_worker",
                f"No worker on task queue {source.task_queue!r} answered in time. Is the "
                f"agent's worker running, with the {source.filesystem!r} filesystem "
                "registered?",
            ) from e
        raise FileViewError(502, "view_failed", str(cause)) from e
