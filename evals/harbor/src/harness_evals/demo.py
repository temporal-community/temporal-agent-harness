"""Launch this checkout's example workers for the local eval demo."""

import json
import os
import sys
from pathlib import Path

AGENTS = {
    "openai": ("OpenAIHelloAgent", "openai-hello", "openai_hello"),
    "pydantic": ("PydanticAIHelloAgent", "pydantic-ai-hello", "pydantic_ai_hello"),
}


def worker(sdk: str, address: str, namespace: str, examples_repo: Path | None = None) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY before starting the example worker")
    if examples_repo is None:
        examples_repo = Path(__file__).resolve().parents[4]
    source_path = examples_repo.resolve() / "python"
    if not (source_path / "examples").is_dir():
        raise ValueError("Run this experimental project from an Agent Kit repository checkout")
    _, queue, module = AGENTS[sdk]
    # This convenience launcher is explicitly for the local demo. Do not inherit an
    # unrelated Temporal Cloud API key, TLS setting, or connection profile.
    env = {key: value for key, value in os.environ.items() if not key.startswith("TEMPORAL_")}
    env.pop("TASK_QUEUE_PREFIX", None)
    config_path = Path(".cache/demo-client.toml").resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[profile.harness_eval]\n"
        f"address = {json.dumps(address)}\nnamespace = {json.dumps(namespace)}\n"
        "[profile.harness_eval.tls]\ndisabled = true\n"
    )
    env.update(
        {
            "PYTHONPATH": str(source_path),
            "TEMPORAL_ADDRESS": address,
            "TEMPORAL_NAMESPACE": namespace,
            "TEMPORAL_CONFIG_FILE": str(config_path),
            "TEMPORAL_PROFILE": "harness_eval",
            "OPENAI_AGENTS_DISABLE_TRACING": "1",
            "OPENAI_HELLO_TASK_QUEUE": queue,
            "PYDANTIC_AI_HELLO_TASK_QUEUE": queue,
        }
    )
    os.execvpe(sys.executable, [sys.executable, "-m", "harness_evals.demo_entry", sdk], env)
