# ABOUTME: Public surface of the client-side stream-merge — the layer that lets AgentClient's
# send_message / attach present a parent agent and all its (recursive, possibly concurrent)
# subagents as ONE logical event stream, while each agent keeps its own independent workflow
# stream (stream isolation is never violated — the merge only READS each stream). See
# docs/internal/unified-subagent-event-stream.md for the full design; gates.py holds the load-bearing
# happens-before invariants.

from temporal_agent_harness.harness.stream_merge.gates import (
    Gates,
    MountChild,
    UnmountChild,
)
from temporal_agent_harness.harness.stream_merge.merge import (
    DEFAULT_STALL_GRACE_SECONDS,
    SelectPolicy,
    ShouldStop,
    merge_stream,
    select_live,
    select_replay,
)

__all__ = [
    "merge_stream",
    "select_live",
    "select_replay",
    "SelectPolicy",
    "ShouldStop",
    "DEFAULT_STALL_GRACE_SECONDS",
    "Gates",
    "MountChild",
    "UnmountChild",
]
