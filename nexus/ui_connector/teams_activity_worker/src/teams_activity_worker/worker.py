"""Activities-only Temporal worker process."""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import TeamsActivities
from .config import Settings
from .platform import TeamsPlatform


async def run() -> None:
    settings = Settings.from_env()
    platform = TeamsPlatform.from_settings(settings)
    if platform.app is not None:
        await platform.app.initialize()

    temporal = await Client.connect(settings.temporal_address, namespace=settings.connector_namespace)
    activities = TeamsActivities(platform)
    worker = Worker(
        temporal,
        task_queue=settings.task_queue,
        activities=[
            activities.begin_stream,
            activities.update_stream,
            activities.finish_stream,
            activities.post_message,
            activities.post_approval_prompt,
            activities.update_activity,
        ],
    )
    logging.info("Starting Teams activity worker on task queue %r", settings.task_queue)
    try:
        await worker.run()
    finally:
        if platform.app is not None:
            await platform.app.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())
