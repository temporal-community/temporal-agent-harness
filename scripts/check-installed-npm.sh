#!/usr/bin/env bash
# Installs the built npm tarballs (each public package's :pkg_tgz) into an empty project and proves
# they work outside the workspace: they install together from the registry's point of view, the
# bindings resolve the client tarball rather than fetching another client, and a TypeScript
# consumer of every binding type-checks. tests.yml runs this on every change; it is the "install
# the tarballs in a clean project" release check, automated.
#
# Usage, from the repository root: scripts/check-installed-npm.sh <directory holding the tarballs>
# The directory holds one tarball per public package, named client.tgz, react.tgz, svelte.tgz.
set -euo pipefail

tarballs="$(cd "${1:?usage: $0 <directory holding the tarballs>}" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

echo "Installing $(ls "$tarballs") into an empty project"
npm init -y >/dev/null
# The bindings' peers, and the TypeScript the client was built with.
typescript="$(tar -xzOf "$tarballs/client.tgz" package/package.json | node -p 'JSON.parse(require("fs").readFileSync(0)).devDependencies.typescript')"
npm install --no-package-lock --no-audit --no-fund --loglevel=error \
  "$tarballs"/*.tgz react@19 react-dom@19 @types/react@19 svelte@5 "typescript@$typescript"

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

echo "A TypeScript consumer of every package type-checks"
cat > probe.ts <<'TS'
import { AgentSessionCore, startSession } from "@temporalio/agent-harness-client";
import { HarnessProvider, ReactSessionState, useAgentSession } from "@temporalio/agent-harness-react";
import { AgentSession, SvelteSessionState, createHarnessContext } from "@temporalio/agent-harness-svelte";

export const exports = [
  AgentSessionCore, startSession,
  HarnessProvider, ReactSessionState, useAgentSession,
  AgentSession, SvelteSessionState, createHarnessContext,
];
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
