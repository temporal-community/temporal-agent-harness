#!/bin/sh
set -eu

# The same entrypoint runs inside Harbor and when rescoring a saved recording.
tests_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
recording_dir=${HARNESS_EVAL_RECORDING_DIR:-/logs/agent}
reward_path=${HARNESS_EVAL_REWARD_PATH:-/logs/verifier/reward.json}
export LITELLM_LOCAL_MODEL_COST_MAP=True
cd "$recording_dir"

if [ "${HARNESS_EVAL_JUDGE:-0}" = "1" ]; then
    exec python -m rewardkit "$tests_dir" --workspace . --output "$reward_path"
fi
exec python -m rewardkit "$tests_dir/behavior" --workspace . --output "$reward_path"
