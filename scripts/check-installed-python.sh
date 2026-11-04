#!/usr/bin/env bash
# Installs the built wheel (//python:dists) into an empty virtual environment and proves it works
# without the repository: the CLI starts, every integration imports, and the conformance scenarios
# run against the installed package on a local Temporal dev server. tests.yml runs this on every
# change; it is the "install the wheel in a clean project" release check, automated.
#
# Usage, from the repository root: scripts/check-installed-python.sh <directory holding the wheel>
# PYTHON_VERSION selects the interpreter (default 3.13). TEMPORAL_CLI_PATH, if set, names a
# `temporal` CLI for the dev server; otherwise the Temporal SDK downloads one.
set -euo pipefail

dists="$(cd "${1:?usage: $0 <directory holding the wheel>}" && pwd)"
wheel="$(ls "$dists"/temporal_agent_harness-*.whl)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Installing $(basename "$wheel") into an empty environment"
uv venv --quiet --python "${PYTHON_VERSION:-3.13}" "$work/venv"
# Every extra a user can install from PyPI alone (nexus-mcp and typesafe need other sources), plus
# what the conformance tests need. Third-party versions are held to python/uv.lock's, the
# combination the test suite runs against; the lockfile's editable workspace entries are not
# constraints.
(cd python && uv export --frozen --no-hashes --no-emit-project --all-extras --quiet \
  | grep -v '^-e ' > "$work/constraints.txt")
uv pip install --quiet --python "$work/venv" --constraint "$work/constraints.txt" \
  "temporal-agent-harness[ui,genai,openai-agents,pydantic-ai,code-mode] @ file://$wheel" \
  pytest pytest-asyncio jsonschema
venv="$work/venv/bin"

echo "The CLI runs"
"$venv/temporal-agent-harness" --help >/dev/null
"$venv/temporal-agent-harness" serve --help >/dev/null
"$venv/temporal-agent-harness" session-manager --help >/dev/null

echo "Every integration imports from the install"
(cd / && "$venv/python" - <<'PY'
import importlib
for name in [
    "temporal_agent_harness.harness",
    "temporal_agent_harness.harness.code_mode",
    "temporal_agent_harness.ai_sdks.google_genai_plugin",
    "temporal_agent_harness.ai_sdks.openai_agents_harness",
    "temporal_agent_harness.ai_sdks.pydantic_ai_harness",
    "temporal_agent_harness.web.serve",
    "temporal_agent_harness.web.session_manager_worker",
]:
    importlib.import_module(name)
from temporal_agent_harness.ui import packaged_ui_dist
assert packaged_ui_dist() is not None, "the install has no console"
PY
)

echo "The packaged starter generates schemas, installs its skill, and runs without a model"
"$venv/temporal-agent-harness" init "$work/starter"
(cd "$work/starter" && "$venv/temporal-agent-harness" skills install)
(cd "$work/starter" && "$venv/temporal-agent-harness" schema my_agent.workflow:OpenAIHelloAgentWorkflow -o agent.schema.json)
"$venv/temporal-agent-harness" schema --openapi -o "$work/openapi.json"
(cd "$work/starter" && "$venv/python" - <<'PROBE'
import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from temporalio.testing import WorkflowEnvironment
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig

assert json.loads(Path("agent.schema.json").read_text())["agent"] == "OpenAIHelloAgent"
assert Path(".agents/skills/temporal-harness-python/references/api.md").is_file()

async def check():
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=os.environ.get("TEMPORAL_CLI_PATH")
    ) as env:
        child_env = dict(os.environ, TEMPORAL_ADDRESS=env.client.service_client.config.target_host,
                         TEMPORAL_NAMESPACE="default", OPENAI_API_KEY="unused-ping-test")
        child_env.pop("TASK_QUEUE_PREFIX", None)
        child_env.pop("TEMPORAL_CONFIG_FILE", None)
        child_env.pop("TEMPORAL_PROFILE", None)
        with Path("worker.log").open("w+") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "my_agent.worker", env=child_env, stdout=log, stderr=log)
            try:
                await env.client.start_workflow("OpenAIHelloAgent", AgentConfig(),
                                                id="installed-starter", task_queue="openai-hello")
                agent = AgentClient(env.client, "installed-starter")
                async def ping():
                    stream = await agent.send_message("ping", {"text": "installed starter works"},
                                                       on_item=lambda event, offset: event)
                    events = [event async for event in stream]
                    assert any(getattr(getattr(event, "event", None), "type", None) == "message_handler_end"
                               and "installed starter works" in str(event) for event in events), events
                await asyncio.wait_for(ping(), 60)
                await env.client.get_workflow_handle("installed-starter").terminate()
            except BaseException:
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                if process.returncode is None:
                    process.send_signal(signal.SIGINT)
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    process.kill()
                    await process.wait()
asyncio.run(check())
PROBE
)

# The conformance tests, from a copy of python/tests so that python/ (and the source tree's
# temporal_agent_harness beside it) is not on sys.path. They read conformance/ and contract/
# relative to the repository root, so run them from there.
echo "The conformance scenarios run against the installed package"
mkdir -p "$work/tests/conformance"
cp python/tests/__init__.py python/tests/conftest.py "$work/tests/"
cp python/tests/conformance/*.py "$work/tests/conformance/"
location="$(PYTHONPATH="$work" "$venv/python" -c 'import temporal_agent_harness; print(temporal_agent_harness.__file__)')"
# Python reports the resolved path; macOS's mktemp hands out one through a symlink.
case "$location" in
  "$work/venv/"* | "$(cd "$work" && pwd -P)/venv/"*) ;;
  *) echo "temporal_agent_harness imported from $location, not from the install" >&2; exit 1 ;;
esac
PYTHONPATH="$work" "$venv/python" -m pytest -q -p no:cacheprovider \
  -o asyncio_mode=auto -o consider_namespace_packages=true --rootdir "$work" \
  "$work/tests/conformance/test_goldens.py"
