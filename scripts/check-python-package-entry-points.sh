#!/usr/bin/env bash
# Fails when a library under python/temporal_agent_kit/ is not a dependency of
# //python/temporal_agent_kit, the target the wheel and sdist ship (//python:dists). A module
# nothing else in the package imports, such as a new SDK integration, is an entry point and has
# to be listed in that target's deps, or it is left out of the release without anything failing.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

unshipped="$(bazel query --noshow_progress --ui_event_filters=-info \
  'kind("py_library", //python/temporal_agent_kit/...) - deps(//python/temporal_agent_kit)')"
if [[ -n "$unshipped" ]]; then
  while IFS= read -r label; do
    echo "::error file=python/temporal_agent_kit/BUILD.bazel::$label is not shipped: add it to //python/temporal_agent_kit's deps"
  done <<<"$unshipped"
  exit 1
fi
