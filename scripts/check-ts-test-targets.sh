#!/usr/bin/env bash
# Fails when a TypeScript test file under typescript/ has no Bazel test target, which would leave it
# out of `bazel test //...` without anything failing. The TypeScript BUILD files are written by hand
# (Gazelle cannot generate them yet), so this stands in for the check Gazelle would give.
#
# A test file is covered when a test rule in its Bazel package names it, relative to the package,
# with any extension (a node --test target's dist/test/foo.test.js covers test/foo.test.ts).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

declare -A tests_in
missing=0
while IFS= read -r file; do
  pkg="$(dirname "$file")"
  until [[ -f "$pkg/BUILD.bazel" ]]; do pkg="$(dirname "$pkg")"; done
  if [[ -z "${tests_in[$pkg]+set}" ]]; then
    tests_in[$pkg]="$(bazel query --noshow_progress --ui_event_filters=-info --output=build \
      "tests(//$pkg:all)" 2>/dev/null || true)"
  fi
  stem="${file#"$pkg"/}"
  stem="${stem%.*}"
  if ! grep -qF "$stem." <<<"${tests_in[$pkg]}"; then
    echo "::error file=$file::$file has no test target in //$pkg"
    missing=1
  fi
done < <(git ls-files 'typescript/**/*.test.ts' 'typescript/**/*.test.tsx' 'typescript/**/*.test.mjs')

exit "$missing"
