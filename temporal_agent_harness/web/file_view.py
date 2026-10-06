"""Reading one page of a mounted file for the console's file viewer.

A mount backed by an :class:`~temporal_agent_harness.harness.code_mode.ActivityFileSystem` has a
``FileIndex`` whose ``source`` names the filesystem, its config and the task queue its
activities run on. The viewer sends that source back with a path, and this runs the
filesystem's ``vfs.<name>.view`` activity as a standalone activity: no workflow is involved, so
viewing a file adds nothing to the agent's history and works after the agent has closed.

What comes back is the file as it is in the store right now, not as it was at any earlier point
in the agent's history. Only ``view`` is ever called, never an operation that changes a file;
the config is the filesystem's to validate.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import PurePosixPath

from pydantic import BaseModel
from temporalio.client import ActivityFailureError, Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, TimeoutError
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.code_mode.activity_fs import (
    MAX_VIEW_BYTES,
    FileChunk,
    FsRead,
    activity_name,
)
from temporal_agent_harness.harness.code_mode.vfs import IndexSource

_FILESYSTEM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")

# Long enough for a worker to pick the call up and read a page; short enough that a missing
# worker fails the view instead of leaving it waiting.
_TIMEOUT = timedelta(seconds=15)

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


class FileViewError(Exception):
    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error


async def view_file(client: Client, request: FileViewRequest) -> FileChunk:
    """Read one page (at most ``MAX_VIEW_BYTES``) of ``request.path``."""
    source = request.source
    if not _FILESYSTEM_NAME.fullmatch(source.filesystem):
        raise FileViewError(400, "invalid_filesystem", f"no filesystem {source.filesystem!r}")
    try:
        return await client.execute_activity(
            activity_name(source.filesystem, "view"),
            FsRead(
                config=dict(source.config),
                path=request.path,
                offset=request.offset,
                length=MAX_VIEW_BYTES,
            ),
            id=f"vfs-view-{uuid.uuid4()}",
            task_queue=source.task_queue,
            result_type=FileChunk,
            schedule_to_close_timeout=_TIMEOUT,
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
                f"No worker on task queue {source.task_queue!r} read the file in time. Is the "
                f"agent's worker running, with the {source.filesystem!r} filesystem "
                "registered?",
            ) from e
        raise FileViewError(502, "view_failed", str(cause)) from e
