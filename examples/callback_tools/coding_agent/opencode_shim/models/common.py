"""Common/shared models used across multiple domains."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field

from .._time import now_ms
from .base import OpenCodeBaseModel


if TYPE_CHECKING:
    from pydantic_ai import RequestUsage, RunUsage
    from pydantic_ai.usage import UsageBase

    FileChange = object  # agentpool-only helper, unused in shim

FileDiffStatus = Literal["added", "deleted", "modified"]


class TimeCreatedUpdated(OpenCodeBaseModel):
    """Timestamp with created and updated fields (milliseconds)."""

    created: int
    updated: int
    archived: int | None = None
    """Timestamp when archived (ms since epoch), or None if not archived."""


class TimeCreated(OpenCodeBaseModel):
    """Timestamp with created field only (milliseconds)."""

    created: int

    @classmethod
    def now(cls) -> Self:
        return cls(created=now_ms())


class TimeStartEnd(OpenCodeBaseModel):
    """Timestamp with start and optional end (milliseconds)."""

    start: int
    end: int | None = None


class ModelRef(OpenCodeBaseModel):
    """Reference to a provider model (provider_id + model_id)."""

    provider_id: str
    model_id: str


class TokenCache(OpenCodeBaseModel):
    """Prompt-cache token counts."""

    read: int = 0
    write: int = 0

    def add(self, tokens: TokenCache) -> None:
        self.read += tokens.read
        self.write += tokens.write


class Tokens(OpenCodeBaseModel):
    """Token usage for one assistant message.

    The TUI computes context-window fill as
    ``input + output + reasoning + cache.read + cache.write``.
    """

    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache: TokenCache = Field(default_factory=TokenCache)
    total: int | None = None

    def add(self, tokens: Tokens) -> None:
        self.input += tokens.input
        self.output += tokens.output
        self.reasoning += tokens.reasoning
        self.cache.add(tokens.cache)
        self.total = (self.total or 0) + (tokens.total or 0)

    @classmethod
    def from_pydantic_ai(cls, usage: UsageBase) -> Tokens:
        """Create from a pydantic-ai Usage object.

        Args:
            usage: pydantic-ai request usage with token counts.
        """
        reasoning = usage.details.get("reasoning_tokens", 0)
        return cls(
            input=usage.input_tokens,
            output=usage.output_tokens,
            reasoning=reasoning,
            cache=TokenCache(read=usage.cache_read_tokens, write=usage.cache_write_tokens),
            total=usage.total_tokens + reasoning,
        )

    def to_request_usage(self) -> RequestUsage:
        """Convert to a pydantic-ai Usage object for request usage."""
        from pydantic_ai import RequestUsage

        return RequestUsage(
            input_tokens=self.input,
            output_tokens=self.output,
            cache_read_tokens=self.cache.read,
            cache_write_tokens=self.cache.write,
        )

    def to_run_usage(self) -> RunUsage:
        """Convert to a pydantic-ai RunUsage object for run usage."""
        from pydantic_ai import RunUsage

        return RunUsage(
            input_tokens=self.input,
            output_tokens=self.output,
            cache_read_tokens=self.cache.read,
            cache_write_tokens=self.cache.write,
        )


class APIErrorData(OpenCodeBaseModel):
    """Data for API errors."""

    message: str
    status_code: int | None = None
    is_retryable: bool = False
    response_headers: dict[str, str] | None = None
    response_body: str | None = None
    metadata: dict[str, str] | None = None


class APIError(OpenCodeBaseModel):
    """API error."""

    name: Literal["APIError"] = Field(default="APIError", init=False)
    data: APIErrorData


class TextSpan(OpenCodeBaseModel):
    """A text span in user input (value + start/end offsets)."""

    value: str
    start: int
    end: int


class FileDiff(OpenCodeBaseModel):
    """A file diff entry."""

    file: str
    before: str
    after: str
    additions: int
    deletions: int
    status: FileDiffStatus | None = None

    @classmethod
    def from_file_change(cls, change: FileChange) -> Self:
        """Create a FileDiff from a FileChange."""
        diff_text = change.to_unified_diff()
        match change.operation:
            case "create":
                status: FileDiffStatus | None = "added"
            case "delete":
                status = "deleted"
            case "edit" | "write":
                status = "modified"
            case _:
                status = None
        return cls(
            file=change.path,
            before=change.old_content or "",
            after=change.new_content or "",
            additions=diff_text.count("\n+"),
            deletions=diff_text.count("\n-"),
            status=status,
        )
