"""Session related models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import OpenCodeBaseModel
from .common import (  # noqa: TC001
    FileDiff,
    TimeCreatedUpdated,
)


SessionStatusType = Literal["idle", "busy", "retry"]


class SessionSummary(OpenCodeBaseModel):
    """Summary information for a session."""

    additions: int | None = None
    deletions: int | None = None
    files: int | None = None
    diffs: list[FileDiff] = Field(default_factory=list)


class SessionRevert(OpenCodeBaseModel):
    """Revert information for a session."""

    message_id: str
    diff: str | None = None
    part_id: str | None = None
    snapshot: str | None = None


class SessionShare(OpenCodeBaseModel):
    """Share information for a session."""

    url: str


class Session(OpenCodeBaseModel):
    """Session information."""

    id: str
    project_id: str
    directory: str
    title: str
    version: str = "1"
    time: TimeCreatedUpdated
    parent_id: str | None = None
    summary: SessionSummary | None = None
    revert: SessionRevert | None = None
    share: SessionShare | None = None


class SessionCreateRequest(OpenCodeBaseModel):
    """Request body for creating a session."""

    parent_id: str | None = None
    title: str | None = None


class SessionTimeUpdate(OpenCodeBaseModel):
    """Time fields that can be updated on a session."""

    archived: int | None = None
    """Timestamp when session was archived (ms since epoch), or None to unarchive."""


class SessionUpdateRequest(OpenCodeBaseModel):
    """Request body for updating a session."""

    title: str | None = None
    time: SessionTimeUpdate | None = None


class SessionForkRequest(OpenCodeBaseModel):
    """Request body for forking a session."""

    message_id: str | None = None
    """Optional message ID to fork from. If provided, only messages up to and including
    this message will be copied to the forked session. If None, all messages are copied."""


class SessionInitRequest(OpenCodeBaseModel):
    """Request body for initializing a session (creating AGENTS.md)."""

    model_id: str | None = None
    """Optional model ID to use for the init task."""

    provider_id: str | None = None
    """Optional provider ID to use for the init task."""


class SummarizeRequest(OpenCodeBaseModel):
    """Request body for summarizing a session.

    Matches OpenCode's compaction API. If model info is provided, uses that model
    for the summary generation. If 'auto' is True, automatically selects the model.
    """

    model_id: str | None = None
    """Optional model ID to use for summary generation."""

    provider_id: str | None = None
    """Optional provider ID to use for summary generation."""

    auto: bool | None = None
    """If True, automatically select the model for summarization."""


class SessionStatus(OpenCodeBaseModel):
    """Status of a session."""

    type: SessionStatusType = "idle"


TodoStatus = Literal["pending", "in_progress", "completed", "cancelled"]
"""Well-known todo status values used by OpenCode.

The field accepts any string, but these are the conventional values:
- ``pending``: Task not yet started.
- ``in_progress``: Task currently being worked on.
- ``completed``: Task finished successfully.
- ``cancelled``: Task was cancelled.
"""

TodoPriority = Literal["high", "medium", "low"]
"""Well-known todo priority values used by OpenCode.

The field accepts any string, but these are the conventional values:
- ``high``: High priority.
- ``medium``: Medium priority (default).
- ``low``: Low priority.
"""


class Todo(OpenCodeBaseModel):
    """Todo item for a session."""

    content: str
    """Brief description of the task."""
    status: TodoStatus | str = "pending"
    """Current status of the task."""
    priority: TodoPriority | str = "medium"
    """Priority level of the task."""
