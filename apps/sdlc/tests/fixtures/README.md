# Replay fixture

`legacy_plan_history.json.gz` is a 62-event Temporal history generated with the
Workbench implementation immediately before `sdlc-native-sdk-v1` was introduced,
using published `temporal-agent-harness==0.4.0`. It contains a demo change task
(`sdlc-` followed by 32 `a` characters) waiting for plan approval. It uses the
original `decide` activity and the earlier actionable-failures patch, and has no
provider credentials or real repository content.

`test_sdk_replay.py` replays it with the current workflow and plugin registration.
Keep this historical fixture unchanged when changing workflow execution; a newly
recorded history would not test compatibility with the previous command sequence.
