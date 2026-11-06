"""Small convenience commands over Harbor's job and verifier contracts."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.models.trajectories.trajectory import Trajectory
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, VerifierConfig

from harness_evals.demo import AGENTS, worker


def passing(rewards: dict) -> bool:
    return rewards.get("reward") == 1


async def run(args: argparse.Namespace) -> int:
    verifier_env = {"HARNESS_EVAL_JUDGE": "1" if args.judge else "0"}
    if args.judge:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Set OPENAI_API_KEY for --judge")
        verifier_env.update(OPENAI_API_KEY="${OPENAI_API_KEY}", REWARDKIT_JUDGE=args.judge_model)
    config = JobConfig(
        job_name=args.job_name or f"harness-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
        jobs_dir=args.jobs_dir,
        n_concurrent_trials=1,
        agents=[
            AgentConfig(
                import_path="harness_evals.adapter:TemporalHarnessAgent",
                kwargs={
                    "workflow_type": AGENTS[sdk][0],
                    "task_queue": AGENTS[sdk][1],
                    "address": args.address,
                    "namespace": args.namespace,
                },
            )
            for sdk in args.agent
        ],
        tasks=[TaskConfig(path=path.resolve()) for path in args.task],
        environment=EnvironmentConfig(type="docker"),
        verifier=VerifierConfig(env=verifier_env),
    )
    job = await Job.create(config)
    await job.run()
    result_paths = sorted((config.jobs_dir / config.job_name).glob("*/result.json"))
    success = bool(result_paths)
    for path in result_paths:
        result = json.loads(path.read_text())
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        ok = result.get("exception_info") is None and passing(rewards)
        success &= ok
        target = result.get("config", {}).get("agent", {}).get("kwargs", {}).get("workflow_type")
        print(f"{'PASS' if ok else 'FAIL'} {target}/{path.parent.name}: {json.dumps(rewards)}")
    print(f"Results: {config.jobs_dir / config.job_name}")
    return 0 if success else 1


def rescore(args: argparse.Namespace) -> int:
    output = args.output or Path("jobs") / f"rescore-{uuid4().hex[:8]}"
    if output.exists():
        raise FileExistsError(output)
    record = json.loads(args.recording.read_text())
    trajectory_path = args.recording.with_name("trajectory.json")
    trajectory = Trajectory.model_validate_json(trajectory_path.read_text()).to_json_dict()
    if trajectory.get("session_id") != record["session_id"]:
        raise ValueError("Recording and trajectory identify different sessions")
    script = (args.task / "tests/test.sh").resolve()
    if not script.is_file():
        raise FileNotFoundError(script)
    if args.judge and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY for --judge")
    output.mkdir(parents=True, exist_ok=False)
    output = output.resolve()
    environment = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "HARNESS_EVAL_RECORDING_DIR": str(args.recording.parent.resolve()),
        "HARNESS_EVAL_REWARD_PATH": str(output / "reward.json"),
        "HARNESS_EVAL_JUDGE": "1" if args.judge else "0",
    }
    if args.judge:
        environment["REWARDKIT_JUDGE"] = args.judge_model
    (output / "rescore.json").write_text(
        json.dumps(
            {
                "recording": str(args.recording.resolve()),
                "task": str(args.task.resolve()),
                "judge": args.judge,
                "judge_model": args.judge_model if args.judge else None,
            },
            indent=2,
        )
        + "\n"
    )
    process = subprocess.run(["sh", str(script)], env=environment, capture_output=True, text=True)
    (output / "test-stdout.txt").write_text(process.stdout)
    (output / "test-stderr.txt").write_text(process.stderr)
    if process.returncode:
        raise RuntimeError(f"Task verifier failed; see {output / 'test-stderr.txt'}")
    rewards = json.loads((output / "reward.json").read_text())
    print(json.dumps(rewards))
    print(f"Saved: {output}")
    return 0 if passing(rewards) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Experimental Agent Kit evals with Harbor")
    sub = parser.add_subparsers(dest="command", required=True)
    launch = sub.add_parser("demo-worker", help="Run an example worker from this checkout")
    launch.add_argument("sdk", choices=list(AGENTS))
    execute = sub.add_parser("run", help="Run real Harbor tasks against example workers")
    execute.add_argument("--agent", choices=list(AGENTS), action="append", required=True)
    execute.add_argument("--task", type=Path, action="append", required=True)
    execute.add_argument("--job-name")
    execute.add_argument("--jobs-dir", type=Path, default=Path("jobs"))
    saved = sub.add_parser("rescore", help="Regrade saved evidence without executing an agent")
    saved.add_argument("recording", type=Path, help="The agent/run.json from a Harbor trial")
    saved.add_argument("--task", type=Path, required=True)
    saved.add_argument("--output", type=Path, help="New directory for rewards, details, and logs")
    for child in [launch, execute]:
        child.add_argument("--address", default="127.0.0.1:7233")
        child.add_argument("--namespace", default="default")
    for child in [execute, saved]:
        child.add_argument("--judge", action="store_true", help="Also call the LLM judge")
        child.add_argument("--judge-model", default="openai/gpt-5.1", help="Rewardkit judge model")
    args = parser.parse_args()
    if args.command == "demo-worker":
        worker(args.sdk, args.address, args.namespace)
    elif args.command == "run":
        raise SystemExit(asyncio.run(run(args)))
    else:
        raise SystemExit(rescore(args))
