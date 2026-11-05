#!/usr/bin/env bash
# Installs the built npm tarballs (each package's :pkg_tgz) into an empty project and proves they
# work outside the workspace: they install together from the registry's point of view, the
# bindings resolve the client tarball rather than fetching another client, a TypeScript consumer
# of every binding type-checks, and the SDK (core and worker) installs beside the Temporal SDK and
# its OpenAI Agents plugin at one Temporal version, loads, and type-checks a worker. tests.yml runs
# this on every change; it is the "install the tarballs in a clean project" release check,
# automated.
#
# Usage, from the repository root: scripts/check-installed-npm.sh <directory holding the tarballs>
# The directory holds one tarball per package, named client.tgz, react.tgz, svelte.tgz, core.tgz
# worker.tgz and codegen.tgz.
set -euo pipefail

tarballs="$(cd "${1:?usage: $0 <directory holding the tarballs>}" && pwd)"
work="$(mktemp -d)"
starter="$(mktemp -d)"
trap 'rm -rf "$work" "$starter"' EXIT
cd "$work"

echo "Installing $(ls "$tarballs" | tr '\n' ' ')into an empty project"
npm init -y >/dev/null
manifest() { tar -xzOf "$tarballs/$1.tgz" package/package.json | node -p "JSON.parse(require('fs').readFileSync(0))$2"; }
# The bindings' peers, and the TypeScript the client was built with.
typescript="$(manifest client .devDependencies.typescript)"
# Every @temporalio/* package at ONE version, the one @temporalio/openai-agents was built for: that
# plugin pins its Temporal siblings exactly while the worker's ranges float, so an unpinned install
# can resolve two Temporal SDKs side by side, which fails to type-check and crashes the worker on
# load (duplicate protobuf namespaces). A user must pin the same way; the SDK docs say so.
openai_agents="$(manifest worker '.peerDependencies["@temporalio/openai-agents"]')"
temporal="$(npm view "@temporalio/openai-agents@$openai_agents" 'dependencies.@temporalio/worker' | tail -1)"
echo "Temporal SDK $temporal, from @temporalio/openai-agents@$openai_agents"
temporal_packages=()
for name in activity client common envconfig plugin testing worker workflow workflow-streams; do
  temporal_packages+=("@temporalio/$name@$temporal")
done
npm install --no-package-lock --no-audit --no-fund --loglevel=error \
  "$tarballs"/*.tgz react@19 react-dom@19 @types/react@19 svelte@5 "typescript@$typescript" @types/node@22 \
  "@temporalio/openai-agents@$temporal" "${temporal_packages[@]}" \
  "@openai/agents-core@$(manifest worker '.peerDependencies["@openai/agents-core"]')" \
  "@openai/agents-openai@$(manifest worker '.peerDependencies["@openai/agents-openai"]')" \
  "openai@$(manifest worker .peerDependencies.openai)"

echo "One Temporal SDK: no package installed its own copy"
nested="$(find node_modules -path '*/node_modules/@temporalio/*/node_modules/@temporalio' -maxdepth 6)"
if [ -n "$nested" ]; then
  echo "nested Temporal packages, so two SDK versions are installed:" >&2; echo "$nested" >&2; exit 1
fi

echo "The bindings use the installed client, at one version"
version="$(node -p "require('./node_modules/@temporalio/agent-harness-client/package.json').version")"
for binding in react svelte; do
  installed="$(node -p "require('./node_modules/@temporalio/agent-harness-$binding/package.json').version")"
  if [ "$installed" != "$version" ]; then
    echo "@temporalio/agent-harness-$binding is $installed but the client is $version" >&2; exit 1
  fi
  nested="node_modules/@temporalio/agent-harness-$binding/node_modules/@temporalio/agent-harness-client"
  if [ -e "$nested" ]; then
    echo "@temporalio/agent-harness-$binding installed its own client instead of the tarball's" >&2; exit 1
  fi
done

echo "The SDK and its OpenAI Agents plugin load from the install"
node --input-type=module -e '
import "@temporalio/agent-harness";
import "@temporalio/agent-harness/ai-sdks/openai-agents-harness";
import "@temporalio/agent-harness-worker";
import "@temporalio/agent-harness-worker/ai-sdks/openai-agents";
import "@temporalio/agent-harness-worker/harness/agent-client";
'

echo "A TypeScript consumer of every package type-checks"
cat > probe.ts <<'TS'
import { AgentSessionCore, startSession } from "@temporalio/agent-harness-client";
import { HarnessProvider, ReactSessionState, useAgentSession } from "@temporalio/agent-harness-react";
import { AgentSession, SvelteSessionState, createHarnessContext } from "@temporalio/agent-harness-svelte";
import { TextMessage, TextReply, ToolApprovalPolicy, defineAgent, handler, tool } from "@temporalio/agent-harness";
import { asOpenAIAgentTool, runStreamed } from "@temporalio/agent-harness/ai-sdks/openai-agents-harness";
import { AgentHarnessPlugin } from "@temporalio/agent-harness-worker";
import { OpenAIAgentsPlugin } from "@temporalio/agent-harness-worker/ai-sdks/openai-agents";
import { AgentClient } from "@temporalio/agent-harness-worker/harness/agent-client";
import type { WorkerOptions } from "@temporalio/worker";

export const exports = [
  AgentSessionCore, startSession,
  HarnessProvider, ReactSessionState, useAgentSession,
  AgentSession, SvelteSessionState, createHarnessContext,
  TextMessage, TextReply, ToolApprovalPolicy, defineAgent, handler, tool,
  asOpenAIAgentTool, runStreamed, AgentClient,
];
// A worker registering both plugins: the line a second Temporal SDK version fails on.
export const plugins: WorkerOptions["plugins"] = [new OpenAIAgentsPlugin(), new AgentHarnessPlugin()];
TS
cat > tsconfig.json <<'JSON'
{
  "compilerOptions": {
    "strict": true,
    "noEmit": true,
    "module": "esnext",
    "target": "es2022",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "skipLibCheck": true,
    "types": []
  },
  "files": ["probe.ts"]
}
JSON
./node_modules/.bin/tsc -p tsconfig.json
echo "Installed packages work at $version"

echo "The installed SDK creates a starter, activates its skill, and generates application types"
./node_modules/.bin/agent-harness init "$starter"
# A sibling directory prevents the parent probe's node_modules from hiding a missing dependency.
cd "$starter"
starter_temporal="$(node -p 'JSON.parse(require("fs").readFileSync("package.json")).dependencies["@temporalio/worker"]')"
npm install --no-package-lock --no-audit --no-fund --loglevel=error \
  "$tarballs"/*.tgz "@temporalio/testing@$starter_temporal"
npm run schema
npm run harness:skill
./node_modules/.bin/harness-codegen agent.schema.json -o Agent.ts
./node_modules/.bin/tsc --ignoreConfig --noEmit --skipLibCheck --module nodenext --target es2022 Agent.ts
node --input-type=module - <<'PROBE'
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { existsSync, readFileSync } from 'node:fs';
import { TestWorkflowEnvironment } from '@temporalio/testing';
import { AgentClient } from '@temporalio/agent-harness-worker/harness/agent-client';
assert(existsSync('.agents/skills/temporal-harness-typescript/references/api.md'));
const schema = JSON.parse(readFileSync('agent.schema.json', 'utf8'));
assert.equal(schema.agent, 'OpenAIHelloAgentTs');
const env = await TestWorkflowEnvironment.createLocal({
  server: process.env.TEMPORAL_CLI_PATH ? {
    executable: { type: 'existing-path', path: process.env.TEMPORAL_CLI_PATH },
  } : {},
});
let logs = '';
const childEnv = { ...process.env, TEMPORAL_ADDRESS: env.address,
  TEMPORAL_NAMESPACE: 'default' };
delete childEnv.OPENAI_API_KEY; // ping must not need a provider key
delete childEnv.TEMPORAL_CONFIG_FILE;
delete childEnv.TEMPORAL_PROFILE;
delete childEnv.TASK_QUEUE_PREFIX;
const worker = spawn(process.execPath, ['dist/worker.js'], { env: childEnv });
worker.stdout.on('data', data => { logs += data; });
worker.stderr.on('data', data => { logs += data; });
const stopped = once(worker, 'exit');
let deadline;
try {
  await Promise.race([new Promise((_, reject) => {
    deadline = setTimeout(() => reject(new Error('Starter ping timed out')), 60000);
  }), (async () => {
  await env.client.workflow.start('OpenAIHelloAgentTs', {
    workflowId: 'installed-starter', taskQueue: 'openai-hello-ts', args: [{}],
  });
  const agent = new AgentClient(env.client, 'installed-starter');
  const events = [];
  for await (const [event] of await agent.sendMessage('ping', { text: 'installed starter works' })) events.push(event);
  assert(events.some(event => event.event?.type === 'message_handler_end' && JSON.stringify(event).includes('installed starter works')));
  await env.client.workflow.getHandle('installed-starter').terminate();
  })()]);
} catch (error) { console.error(logs); throw error; }
finally {
  clearTimeout(deadline);
  worker.kill('SIGINT');
  const force = setTimeout(() => worker.kill('SIGKILL'), 10000);
  await stopped;
  clearTimeout(force);
  await env.teardown();
}
PROBE
echo "Installed starter and generated client work"
