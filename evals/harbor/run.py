"""Edit AGENT to evaluate your own worker; run with `uv run python run.py`."""

import asyncio
from pathlib import Path

from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig

# This is the user-owned configuration. The adapter implementation is shared.
# The model and tools are configured on your worker, which must already be running.
AGENT = AgentConfig(
    import_path="harness_evals.adapter:TemporalHarnessAgent",
    kwargs={
        "address": "127.0.0.1:7233",
        "namespace": "default",
        "workflow_type": "OpenAIHelloAgent",
        "task_queue": "openai-hello",
        "handler": "ask",
        "input_key": "text",
    },
)
ROOT = Path(__file__).resolve().parent


async def main():
    config = JobConfig(
        jobs_dir=ROOT / "jobs",
        n_concurrent_trials=1,
        agents=[AGENT],
        tasks=[TaskConfig(path=ROOT / "tasks/weather")],
        environment=EnvironmentConfig(type="docker"),
    )
    job = await Job.create(config)
    return await job.run()


if __name__ == "__main__":
    asyncio.run(main())
