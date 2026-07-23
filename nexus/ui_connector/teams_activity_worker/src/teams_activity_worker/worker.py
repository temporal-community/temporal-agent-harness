"""Environment configuration and process lifecycle for the Teams activity worker."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from microsoft_teams.api import ApiClient
from microsoft_teams.apps import App
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from .contracts import (
    ApprovalPrompt,
    BeginStream,
    ContractError,
    FinishStream,
    TextMetadata,
    UpdateMessage,
    UpdateStream,
)
from .platform import TeamsPlatform

DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams/"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    microsoft_tenant_id: str
    microsoft_app_id: str
    microsoft_app_password: str
    teams_service_url: str = DEFAULT_SERVICE_URL
    temporal_address: str = "localhost:7233"
    connector_namespace: str = "connector"
    task_queue: str = "nexus-connector-teams"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            microsoft_tenant_id=_required("MICROSOFT_TENANT_ID"),
            microsoft_app_id=_required("MICROSOFT_APP_ID"),
            microsoft_app_password=_required("MICROSOFT_APP_PASSWORD"),
            teams_service_url=os.getenv("TEAMS_SERVICE_URL", DEFAULT_SERVICE_URL).strip() or DEFAULT_SERVICE_URL,
            temporal_address=os.getenv("TEMPORAL_ADDRESS", "localhost:7233").strip() or "localhost:7233",
            connector_namespace=os.getenv("CONNECTOR_NAMESPACE", "connector").strip() or "connector",
            task_queue=os.getenv("CONNECTOR_TASK_QUEUE", "nexus-connector-teams").strip() or "nexus-connector-teams",
        )


def _worker_task_queue(shared_task_queue: str) -> str:
    return f"{shared_task_queue}-stream-{uuid4().hex}"


def _platform_from_settings(settings: Settings, worker_task_queue: str) -> TeamsPlatform:
    app = App(
        client_id=settings.microsoft_app_id,
        client_secret=settings.microsoft_app_password,
        tenant_id=settings.microsoft_tenant_id,
        service_url=settings.teams_service_url,
    )

    def api_factory(service_url: str) -> ApiClient:
        return ApiClient(service_url, app.api.http, cloud=app.cloud)

    return TeamsPlatform(
        app_id=settings.microsoft_app_id,
        default_service_url=settings.teams_service_url,
        api_factory=api_factory,
        worker_task_queue=worker_task_queue,
        app=app,
    )


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
    async def update_message(self, payload: dict[str, Any]) -> None:
        await self.platform.update_message(_parse(UpdateMessage.from_payload, payload))


async def run() -> None:
    settings = Settings.from_env()
    worker_task_queue = _worker_task_queue(settings.task_queue)
    platform = _platform_from_settings(settings, worker_task_queue)
    if platform.app is not None:
        await platform.app.initialize()

    temporal = await Client.connect(settings.temporal_address, namespace=settings.connector_namespace)
    activities = TeamsActivities(platform)
    shared_worker = Worker(
        temporal,
        task_queue=settings.task_queue,
        activities=[
            activities.begin_stream,
            activities.post_message,
            activities.post_approval_prompt,
            activities.update_message,
        ],
    )
    stream_worker = Worker(
        temporal,
        task_queue=worker_task_queue,
        activities=[
            activities.update_stream,
            activities.finish_stream,
        ],
    )
    logging.info(
        "Starting Teams activity worker on shared queue %r and private stream queue %r",
        settings.task_queue,
        worker_task_queue,
    )
    try:
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(shared_worker.run())
            task_group.create_task(stream_worker.run())
    finally:
        if platform.app is not None:
            await platform.app.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
