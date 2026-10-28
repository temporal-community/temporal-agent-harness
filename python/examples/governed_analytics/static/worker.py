"""Worker for the static governed analytics flow: plain Temporal, no agent harness.

Run with `just static-worker` from examples/governed_analytics (Bazel runs it from the repo
root). Connection settings come from a ``temporal.toml`` profile (TEMPORAL_CONFIG_FILE /
TEMPORAL_PROFILE, set in the repo-root .env.local), as for the other examples.

Env vars:
    TEMPORAL_CONFIG_FILE          path to a temporal.toml (set in .env.local)
    TEMPORAL_PROFILE              profile name to load (default: "default")
    GOVERNED_ANALYTICS_DATA_DIR   where the .duckdb files are (default: the example's data/,
                                  written by `just data`)

The flow asks the Dogwood verifier behind the `tool-verifier` Nexus endpoint before every
step (`just endpoint`, `just verifier`). Without it, every step waits for the verifier.
"""

import asyncio
import logging
import sys

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from examples.governed_analytics.databases import DATABASES, data_dir
from examples.governed_analytics.sql_analysis import analyze_sql
from examples.governed_analytics.static.activities import ALL_ACTIVITIES
from examples.governed_analytics.static.workflow import TASK_QUEUE, BranchReport


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    missing = [d for d in DATABASES if not (data_dir() / f"{d}.duckdb").exists()]
    if missing:
        sys.exit(f"error: no {', '.join(missing)} database in {data_dir()}; run `just data`")

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, data_converter=pydantic_data_converter)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[BranchReport],
        activities=[*ALL_ACTIVITIES, analyze_sql],
    )
    print(
        f"Static governed analytics worker ready: "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} taskQueue={TASK_QUEUE} "
        f"data={data_dir()}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
