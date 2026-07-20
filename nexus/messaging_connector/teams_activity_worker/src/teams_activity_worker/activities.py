"""Temporal activity entry points."""

from __future__ import annotations

from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .contracts import (
    ApprovalPrompt,
    BeginStream,
    ContractError,
    FinishStream,
    TextMetadata,
    UpdateActivity,
    UpdateStream,
)
from .platform import TeamsPlatform


def _parse(parser, payload: dict[str, Any]):
    try:
        return parser(payload)
    except (ContractError, TypeError, ValueError) as error:
        raise ApplicationError(str(error), type="InvalidTeamsActivityInput", non_retryable=True) from error


class TeamsActivities:
    def __init__(self, platform: TeamsPlatform) -> None:
        self.platform = platform

    @activity.defn(name="BeginStream")
    async def begin_stream(self, payload: dict[str, Any]) -> dict[str, object]:
        return await self.platform.begin_stream(_parse(BeginStream.from_payload, payload))

    @activity.defn(name="UpdateStream")
    async def update_stream(self, payload: dict[str, Any]) -> None:
        await self.platform.update_stream(_parse(UpdateStream.from_payload, payload))

    @activity.defn(name="FinishStream")
    async def finish_stream(self, payload: dict[str, Any]) -> None:
        await self.platform.finish_stream(_parse(FinishStream.from_payload, payload))

    @activity.defn(name="PostMessage")
    async def post_message(self, payload: dict[str, Any]) -> None:
        await self.platform.post_message(_parse(TextMetadata.from_payload, payload))

    @activity.defn(name="PostApprovalPrompt")
    async def post_approval_prompt(self, payload: dict[str, Any]) -> None:
        await self.platform.post_approval_prompt(_parse(ApprovalPrompt.from_payload, payload))

    @activity.defn(name="UpdateActivity")
    async def update_activity(self, payload: dict[str, Any]) -> None:
        await self.platform.update_activity(_parse(UpdateActivity.from_payload, payload))
