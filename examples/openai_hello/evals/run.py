"""Seed and run the OpenAI Hello eval dataset.

    python -m examples.openai_hello.evals.run seed
    python -m examples.openai_hello.evals.run run --name gpt-5.1-v1
    python -m examples.openai_hello.evals.run run --name quick --case weather-uses-the-tool

Needs a Temporal server, an OpenAI Hello worker (``just worker-openai-hello``),
``OPENAI_API_KEY``, and ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` (plus ``LANGFUSE_HOST``
when self-hosting).

The cross-SDK counterpart to ``examples/monty/evals/run.py``: byte-for-byte the same structure
against a completely different AI SDK, because the dataset format, the runner and the scorers
all live at the harness layer.
"""

from __future__ import annotations

import argparse
import asyncio

from temporalio.client import Client
from temporalio.converter import DataConverter
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.ai_sdks.openai_agents import OpenAIPayloadConverter
from temporal_agent_harness.evals.langfuse import run_experiment, seed_dataset

from examples.openai_hello.evals.dataset import (
    DATASET_DESCRIPTION,
    DATASET_NAME,
    cases,
)
from examples.openai_hello.evals.evaluators import DEFAULT_EVALUATORS


async def _connect() -> Client:
    """A plain client, using the OpenAI worker's payload converter — and NOT traced.

    The converter has to match the worker's or payloads cannot be read back, and this agent's
    worker takes its converter from ``OpenAIAgentsPlugin`` (unlike the Monty worker, which uses
    the pydantic converter plus the large-payload offload codec).

    Tracing is deliberately absent here: this process only sends messages and reads replies, and
    creates no spans. The traces come from the WORKER, where the workflow and its activities
    run (see ``examples/openai_hello/worker.py``). Installing a tracer provider here would look
    like it was doing something and do nothing — so if a run yields scores but no traces, the
    worker is the thing to check.
    """
    connect_config = ClientConfig.load_client_connect_config()
    return await Client.connect(
        **connect_config,
        data_converter=DataConverter(payload_converter_class=OpenAIPayloadConverter),
    )


async def _seed() -> None:
    dataset = cases()
    seed_dataset(DATASET_NAME, dataset, description=DATASET_DESCRIPTION)
    print(f"seeded {len(dataset)} cases into dataset {DATASET_NAME!r}")


async def _run(name: str, case_ids: list[str] | None, concurrency: int) -> None:
    client = await _connect()
    summary = await run_experiment(
        client,
        DATASET_NAME,
        name,
        evaluators=DEFAULT_EVALUATORS,
        item_ids=case_ids,
        max_concurrency=concurrency,
        timeout=300.0,
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
