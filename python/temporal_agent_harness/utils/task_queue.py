"""One environment variable that moves a whole harness deployment onto its own task queues.

Each agent worker polls its own queue, ``agents.toml`` names those queues for the server, and the
session manager has a fixed workflow id and queue. Running a second, isolated copy of all of that
in the same namespace (one per demo workspace, say) means every one of those names has to change
together. Setting ``TASK_QUEUE_PREFIX`` to the same value for every process does that: workers
poll ``<prefix>-<queue>``, the server routes sessions there, and the session manager becomes
``<prefix>-session-manager``. Unset, every name is unchanged.
"""

from __future__ import annotations

import os

TASK_QUEUE_PREFIX_ENV = "TASK_QUEUE_PREFIX"


def task_queue_name(base: str) -> str:
    """``base`` under this process's ``TASK_QUEUE_PREFIX``, or ``base`` itself when it is unset."""
    prefix = os.environ.get(TASK_QUEUE_PREFIX_ENV)
    return f"{prefix}-{base}" if prefix else base
