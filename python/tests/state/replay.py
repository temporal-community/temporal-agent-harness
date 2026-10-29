# ABOUTME: Patch-replay helper for the state tests. jsonpatch inserts an op's `value` into
# the document BY REFERENCE, so a later op in the same patch can mutate an object the op list
# still owns; these tests replay the same ops more than once, so every apply works on a copy.

"""Applying a patch stream in the tests.

``jsonpatch`` inserts an op's ``value`` into the document *by reference*, so a
later op in the same patch can mutate an object that is still owned by the op
list.  The tests replay the same ops more than once, so every application works
on a copy of the ops.
"""

from __future__ import annotations

import copy
from typing import Any, Sequence

import jsonpatch

from temporal_agent_harness.harness.state import StatePatch, StateSnapshot


def apply_ops(document: dict[str, Any], ops: Sequence[dict[str, Any]]) -> Any:
    return jsonpatch.apply_patch(document, copy.deepcopy(list(ops)))


def replay(events: Sequence[StateSnapshot | StatePatch]) -> Any:
    """Version-0 snapshot plus every patch, in order."""
    document = copy.deepcopy(events[0].value)  # type: ignore[union-attr]
    for event in events[1:]:
        document = apply_ops(document, event.ops)  # type: ignore[union-attr]
    return document
