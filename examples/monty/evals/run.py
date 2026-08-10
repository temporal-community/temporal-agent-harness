"""Seed and run the Monty eval dataset.

    python -m examples.monty.evals.run seed
    python -m examples.monty.evals.run run --name gemini-3-flash-v1
    python -m examples.monty.evals.run run --name quick --case book-cheapest-sfo-jfk

Needs a Temporal server, a Monty worker (``just worker-monty``), ``GEMINI_API_KEY``, and
``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` (plus ``LANGFUSE_HOST`` when self-hosting).

Tracing is switched on here, so the traces the agent emits during the run are the same ones the
scores get attached to — there is no separate "eval mode" instrumentation. The agent code is
untouched by any of this.
"""

from __future__ import annotations

import argparse
import asyncio

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.evals import setup_tracing
from temporal_agent_harness.evals.langfuse import run_experiment, seed_dataset
from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from examples.monty.evals.dataset import DATASET_DESCRIPTION, DATASET_NAME, cases
from examples.monty.evals.evaluators import DEFAULT_EVALUATORS


async def _connect(*, traced: bool) -> Client:
    connect_config = ClientConfig.load_client_connect_config()
    plugins = [setup_tracing()] if traced else []
    return await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(pydantic_data_converter),
        plugins=plugins,
    )


async def _seed() -> None:
    dataset = cases()
    seed_dataset(DATASET_NAME, dataset, description=DATASET_DESCRIPTION)
    print(f"seeded {len(dataset)} cases into dataset {DATASET_NAME!r}")


async def _run(name: str, case_ids: list[str] | None, concurrency: int) -> None:
    client = await _connect(traced=True)
    summary = await run_experiment(
        client,
        DATASET_NAME,
        name,
        evaluators=DEFAULT_EVALUATORS,
        item_ids=case_ids,
        max_concurrency=concurrency,
        # Generous: a case is several conversational turns, each doing real model calls plus
        # simulated-latency activities.
        timeout=600.0,
    )
    print(summary.format())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="push the dataset to Langfuse")

    run_parser = sub.add_parser("run", help="run the dataset as an experiment")
    run_parser.add_argument("--name", required=True, help="experiment run name")
    run_parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="run only this case id (repeatable) — the tight loop while fixing one failure",
    )
    run_parser.add_argument("--concurrency", type=int, default=4)

    args = parser.parse_args()
    if args.command == "seed":
        asyncio.run(_seed())
    else:
        asyncio.run(_run(args.name, args.cases, args.concurrency))


if __name__ == "__main__":
    main()
