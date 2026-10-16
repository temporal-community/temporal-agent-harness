# ABOUTME: Points WorkflowEnvironment at pre-installed Temporal servers when the environment names them.
#
# TEMPORAL_CLI_PATH backs start_local (the `temporal` dev server) and TEMPORAL_TEST_SERVER_PATH
# backs start_time_skipping (the Java test server). Bazel sets both to the binaries pinned in
# MODULE.bazel, so a sandboxed test never downloads a server. With neither set, as under a plain
# `uv run pytest`, the SDK downloads them as it always has. An explicit path argument at a call
# site still wins.
#
# MONTY_BIN, read by pydantic-monty, is made absolute for the same reason the server paths are:
# BUILD files name it relative to the runfiles root, and tests change directory.

from __future__ import annotations

import functools
import os
from collections.abc import Awaitable, Callable
from typing import Any

from temporalio.testing import WorkflowEnvironment


def _default_server_path(name: str, parameter: str, env_var: str) -> None:
    path = os.environ.get(env_var)
    if not path:
        return
    path = os.path.abspath(path)
    start: Callable[..., Awaitable[WorkflowEnvironment]] = getattr(WorkflowEnvironment, name).__func__

    @functools.wraps(start)
    async def start_with_path(cls: type[WorkflowEnvironment], *args: Any, **kwargs: Any) -> WorkflowEnvironment:
        kwargs.setdefault(parameter, path)
        return await start(cls, *args, **kwargs)

    setattr(WorkflowEnvironment, name, classmethod(start_with_path))


if os.environ.get("MONTY_BIN"):
    os.environ["MONTY_BIN"] = os.path.abspath(os.environ["MONTY_BIN"])

_default_server_path("start_local", "dev_server_existing_path", "TEMPORAL_CLI_PATH")
_default_server_path("start_time_skipping", "test_server_existing_path", "TEMPORAL_TEST_SERVER_PATH")
