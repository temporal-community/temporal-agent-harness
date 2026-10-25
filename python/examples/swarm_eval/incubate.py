"""Run one product experiment, assess evidence, declare readiness, and prepare a promotion patch.

All files are local review artifacts. This CLI never commits, pushes, applies a patch, or deploys.
Run `bazel run //python/examples/swarm_eval:incubate -- --help` from the repository root.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path
import uuid

from pydantic import BaseModel
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    AgentEvent,
    AgentEventType,
    TURN_EVENTS_TOPIC,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.swarm_eval.incubation import (
    Candidate,
    EvaluationArtifact,
    FitAssessment,
    ReadyDeclaration,
    assess,
    declare_ready,
)
from examples.swarm_eval.models import EvaluationEvidence, fingerprint
from examples.swarm_eval.promotion import prepare_promotion, snapshot


def write_artifact(path: Path, value: BaseModel | str) -> None:
    """Create an immutable review artifact; repeating identical output is harmless."""
    text = value.model_dump_json(indent=2) + "\n" if isinstance(value, BaseModel) else value
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    temporary_path = Path(name)
    try:
        with os.fdopen(descriptor, "w") as temporary:
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            # Same-directory hard link publishes a fully written file without replacing one.
            os.link(temporary_path, path)
        except FileExistsError:
            if path.read_text() != text:
                raise ValueError(f"refusing to replace an existing artifact: {path}") from None
    finally:
        temporary_path.unlink()


async def request(client: Client, session: str, handler: str, payload: dict) -> dict:
    reply = await AgentClient(client, session).submit_message(handler, payload)
    stream = WorkflowStreamClient.create(client, session)
    async for item in stream.subscribe(topics=[TURN_EVENTS_TOPIC], from_offset=reply.accepted_offset, result_type=AgentEvent):
        envelope = item.data
        if envelope.message_id != reply.message_id:
            continue
        if envelope.event.type == AgentEventType.MESSAGE_HANDLER_ERROR:
            raise ValueError(envelope.event.message)
        if envelope.event.type == AgentEventType.MESSAGE_HANDLER_END:
            return envelope.event.output
    raise ValueError("agent closed without returning evidence")


async def evaluate(root: Path, candidate: Candidate, output: Path, *, session: str | None = None, timeout: float = 600, task_queue: str = "swarm-eval") -> None:
    if output.exists():
        raise ValueError(f"output already exists; choose a new experiment path: {output}")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    before = snapshot(root)
    run = candidate.evaluation.model_copy(update={"revision": before.digest})
    client = await Client.connect(**ClientConfig.load_client_connect_config(), plugins=[AgentHarnessPlugin()])
    session = session or f"incubate-{candidate.slug}-{uuid.uuid4()}"
    await client.start_workflow("EvalDispatcher", AgentConfig(), id=session, task_queue=task_queue)
    # Print immediately so an interrupted CLI can still inspect/recover the durable run in the UI.
    print(f"Session: {session}", flush=True)
    print(f"Source snapshot: {before.digest}", flush=True)
    async with asyncio.timeout(timeout):
        report = await request(client, session, "evaluate", run.model_dump(mode="json"))
        evidence = EvaluationEvidence.model_validate(await request(client, session, "export", {"run_id": report["run_id"]}))
    if snapshot(root) != before:
        raise ValueError("checkout changed during evaluation; evidence remains in the session, but rerun before declaring readiness")
    artifact = EvaluationArtifact(candidate=candidate, source=before, evidence=evidence)
    write_artifact(output, artifact)
    print(f"Evaluation: {output}\nEvaluation hash: {fingerprint(artifact)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("evaluate", help="run the candidate's small experiment against running workers")
    run.add_argument("--candidate", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--task-queue", default="swarm-eval", help="task queue of the evaluation worker")
    run.add_argument("--timeout", type=float, default=600)
    run.add_argument("--session", help="optional new dispatcher workflow id for recovery")
    for name in ("assess", "declare-ready", "prepare"):
        command = sub.add_parser(name)
        command.add_argument("--evaluation", type=Path, required=True)
        command.add_argument("--fit", type=Path, required=True)
        if name != "assess":
            command.add_argument("--output", type=Path, required=True)
        if name == "declare-ready":
            command.add_argument("--by", required=True)
            command.add_argument("--rationale", required=True)
        if name == "prepare":
            command.add_argument("--declaration", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "evaluate":
            asyncio.run(evaluate(args.repo, Candidate.model_validate_json(args.candidate.read_text()), args.output, session=args.session, timeout=args.timeout, task_queue=args.task_queue))
            return
        artifact = EvaluationArtifact.model_validate_json(args.evaluation.read_text())
        fit = FitAssessment.model_validate_json(args.fit.read_text())
        if args.command == "assess":
            readiness = assess(artifact, fit)
            print(readiness.model_dump_json(indent=2))
            if not readiness.eligible:
                raise SystemExit(1)
        elif args.command == "declare-ready":
            if snapshot(args.repo) != artifact.source:
                raise ValueError("checkout changed since evaluation; evaluate this revision first")
            declaration = declare_ready(artifact, fit, by=args.by, rationale=args.rationale)
            write_artifact(args.output, declaration)
            print(f"Readiness declaration: {args.output}")
        else:
            declaration = ReadyDeclaration.model_validate_json(args.declaration.read_text())
            patch = prepare_promotion(args.repo, artifact, fit, declaration)
            write_artifact(args.output, patch)
            print(f"Promotion patch: {args.output}\nReview it, then check with: git apply --check {args.output}")
    except (ValueError, OSError, TimeoutError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
