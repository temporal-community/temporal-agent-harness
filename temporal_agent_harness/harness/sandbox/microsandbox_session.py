# ABOUTME: Harness-native sandbox session layer — replaces remote-box for the prototype.
# Session cache + lifecycle (create/start/stop/remove) and tool execution for microsandbox
# microVMs, plus an in-process ``local`` backend for CI (no msb required).

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import os
import subprocess
import tempfile
import traceback
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from temporal_agent_harness.harness.sandbox.config import MicrosandboxBackend
from temporal_agent_harness.harness.sandbox_ref import SandboxRef

_INSTALL_MESSAGE = (
    "microsandbox backend requires the optional `sandbox` extra. "
    "Install with `uv sync --extra sandbox`."
)

try:
    from microsandbox import Sandbox, Snapshot
    from microsandbox.errors import MicrosandboxError
except ImportError:
    Sandbox = None  # type: ignore[misc, assignment]
    Snapshot = None  # type: ignore[misc, assignment]
    MicrosandboxError = Exception  # type: ignore[misc, assignment]

AnyBackend = Literal["local"] | MicrosandboxBackend
_BACKEND_LOCAL = "local"
_BACKEND_MICROSANDBOX = "microsandbox"
_REMOTE_EXECUTION_MODE_ENV_VAR = "REMOTE_EXECUTION_MODE"
_EXECUTION_TEMPLATE = """
import asyncio
import json
import os
import sys
import traceback

def _write_result(result_file: str, data: str):
    with open(result_file, 'w') as f:
        f.write(data)

async def execute():
    result_file = os.environ.get('REMOTE_EXECUTION_RESULT_FILE')
    if not result_file:
        print("Error: REMOTE_EXECUTION_RESULT_FILE environment variable not set", file=sys.stderr)
        sys.exit(1)

    try:
        {import_model}
        {import_func}
        arg = {model_name}.model_validate_json({arg_json})
        res = await {func_name}(arg)
        _write_result(result_file, res.model_dump_json())
    except Exception as e:
        _write_result(result_file, json.dumps({{
            "remote_execution_error": True,
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc(),
        }}))
        print(f"Remote execution failed: {{e}}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(execute())
"""
_HARNESS_BASH = """\
#!/usr/bin/env bash
RESULT_FILE="$(mktemp)"
STDERR_FILE="$(mktemp)"
trap 'rm -f "$RESULT_FILE" "$STDERR_FILE"' EXIT INT TERM
REMOTE_EXECUTION_MODE=1 REMOTE_EXECUTION_RESULT_FILE="$RESULT_FILE" {python_cmd} >/dev/null 2>"$STDERR_FILE" <<'REMOTE_BOX_EOF'
{code}
REMOTE_BOX_EOF
PY_EXIT=$?
if [ -s "$RESULT_FILE" ]; then
    cat "$RESULT_FILE"
    exit 0
fi
cat "$STDERR_FILE" >&2
exit "${{PY_EXIT:-1}}"
"""

_current_session: contextvars.ContextVar[SandboxSession | None] = contextvars.ContextVar(
    "_current_session", default=None
)


class RemoteExecutionError(Exception):
    def __init__(self, error_type: str, error_message: str, tb: str) -> None:
        super().__init__(f"{error_type}: {error_message}")
        self.error_type = error_type
        self.error_message = error_message
        self.traceback = tb


def backend_from_dict(data: dict[str, Any]) -> AnyBackend:
    if data.get("type") == _BACKEND_LOCAL:
        return "local"
    return MicrosandboxBackend.model_validate(data)


def is_local_backend(backend: AnyBackend | dict[str, Any]) -> bool:
    if backend == "local":
        return True
    if isinstance(backend, dict):
        return backend.get("type") == _BACKEND_LOCAL
    return False


def _sandbox_name_for_run(run_id: str) -> str:
    # microsandbox names: max 128 UTF-8 bytes; workflow run id is unique and stable.
    name = f"harness-{run_id}"
    encoded = name.encode("utf-8")
    if len(encoded) <= 128:
        return name
    return encoded[:128].decode("utf-8", errors="ignore")


def _get_import_path(obj: Any, local_project_root: Path) -> str:
    module = obj.__module__
    if module == "__main__":
        source_file = inspect.getsourcefile(obj)
        if not source_file:
            raise ValueError(f"Cannot determine source file for {obj}")
        relative = Path(source_file).resolve().relative_to(local_project_root.resolve())
        module = str(relative.with_suffix("")).replace("/", ".").replace("\\", ".")
    return f"from {module} import {obj.__name__}"


def _parse_tool_response(output_model_class: type[BaseModel], stdout_str: str) -> BaseModel:
    try:
        payload = json.loads(stdout_str)
        if isinstance(payload, dict) and payload.get("remote_execution_error"):
            raise RemoteExecutionError(
                payload.get("error_type", "Error"),
                payload.get("error_message", ""),
                payload.get("traceback", ""),
            )
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(payload, dict) and payload.get("remote_execution_error"):
            raise RemoteExecutionError(
                payload.get("error_type", "Error"),
                payload.get("error_message", ""),
                payload.get("traceback", ""),
            )
    try:
        return output_model_class.model_validate_json(stdout_str)
    except ValidationError as exc:
        raise ValueError(f"sandbox tool returned invalid output: {stdout_str!r}") from exc


def _format_harness(python_code: str) -> str:
    return _HARNESS_BASH.format(python_cmd="python3", code=python_code)


class SandboxSession:
    """Common session interface for local and microsandbox backends."""

    def __init__(
        self,
        backend: AnyBackend,
        local_project_root: Path,
        sandbox_name: str | None = None,
    ) -> None:
        self._backend = backend
        self._local_project_root = local_project_root
        self._name = sandbox_name
        self._microsandbox: Any = None
        self._started = False
        self._paused = False
        self._scopes: list[tuple[contextvars.Token[SandboxSession | None], bool]] = []

    @property
    def ref(self) -> SandboxRef:
        if is_local_backend(self._backend):
            return SandboxRef(backend="LOCAL", sandbox_id=None)
        return SandboxRef(backend="MICROSANDBOX", sandbox_id=self._name)

    async def start(self) -> SandboxSession:
        if self._started:
            return self
        if is_local_backend(self._backend):
            self._started = True
            return self
        if Sandbox is None:
            raise RuntimeError(_INSTALL_MESSAGE)
        cfg = self._backend
        assert isinstance(cfg, MicrosandboxBackend)
        name = self._name or _sandbox_name_for_run(str(uuid.uuid4()))
        self._name = name
        kwargs: dict[str, Any] = {"name": name, "detached": True, "replace": True}
        if cfg.snapshot_name:
            kwargs["from_snapshot"] = cfg.snapshot_name
        elif cfg.image:
            kwargs["image"] = cfg.image
        else:
            raise ValueError("MicrosandboxBackend requires snapshot_name or image")
        if cfg.cpus is not None:
            kwargs["cpus"] = cfg.cpus
        if cfg.memory is not None:
            kwargs["memory"] = cfg.memory
        if cfg.env:
            kwargs["env"] = cfg.env
        if cfg.secrets:
            from microsandbox.types import SecretEntry

            kwargs["secrets"] = [SecretEntry(name=k, value=v) for k, v in cfg.secrets.items()]
        self._microsandbox = await Sandbox.create(**kwargs)
        self._started = True
        self._paused = False
        return self

    async def _resume_if_paused(self) -> None:
        if not self._paused or is_local_backend(self._backend):
            return
        if Sandbox is None or self._name is None:
            return
        self._microsandbox = await Sandbox.start(self._name, detached=True)
        self._paused = False

    async def pause(self) -> SandboxRef:
        if is_local_backend(self._backend):
            return self.ref
        if self._microsandbox is not None and self._started:
            await self._microsandbox.stop()
            self._paused = True
        return self.ref

    async def close(self) -> None:
        if is_local_backend(self._backend):
            self._started = False
            return
        if self._microsandbox is not None:
            try:
                await self._microsandbox.stop()
            except MicrosandboxError:
                pass
        if self._name and Sandbox is not None:
            try:
                await Sandbox.remove(self._name)
            except MicrosandboxError:
                pass
        self._microsandbox = None
        self._started = False
        self._paused = False

    async def run_code(self, python_code: str, timeout_millis: int | None = None) -> str:
        if is_local_backend(self._backend):
            return await asyncio.to_thread(_run_code_locally, python_code, timeout_millis)
        await self._resume_if_paused()
        if self._microsandbox is None:
            raise RuntimeError("SandboxSession is not started")
        script = _format_harness(python_code)
        timeout = (timeout_millis / 1000.0) if timeout_millis else None
        output = await self._microsandbox.exec("bash", ["-c", script], timeout=timeout)
        if output.exit_code != 0 and not output.stdout_text.strip():
            raise RuntimeError(
                f"sandbox exec failed (exit {output.exit_code}): {output.stderr_text}"
            )
        return output.stdout_text

    async def __aenter__(self) -> SandboxSession:
        owns = not self._started
        if owns:
            await self.start()
        else:
            await self._resume_if_paused()
        token = _current_session.set(self)
        self._scopes.append((token, owns))
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        token, _owns = self._scopes.pop()
        _current_session.reset(token)


def _run_code_locally(python_code: str, timeout_millis: int | None) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as script_file:
        script_file.write(python_code)
        script_path = script_file.name
    result_file = tempfile.NamedTemporaryFile(delete=False).name
    stderr_file = tempfile.NamedTemporaryFile(delete=False).name
    env = {
        **os.environ,
        _REMOTE_EXECUTION_MODE_ENV_VAR: "1",
        "REMOTE_EXECUTION_RESULT_FILE": result_file,
    }
    try:
        proc = subprocess.run(
            [os.environ.get("PYTHON", "python3"), script_path],
            env=env,
            capture_output=True,
            text=True,
            timeout=(timeout_millis / 1000.0) if timeout_millis else None,
        )
        if os.path.getsize(result_file) > 0:
            with open(result_file) as f:
                return f.read()
        if proc.stderr:
            raise RuntimeError(proc.stderr)
        raise RuntimeError(f"local sandbox exec failed (exit {proc.returncode})")
    finally:
        for path in (script_path, result_file, stderr_file):
            try:
                os.unlink(path)
            except OSError:
                pass


async def get_or_resume_session(
    ref: SandboxRef | None,
    backend: AnyBackend,
    local_project_root: Path,
    *,
    workflow_run_id: str | None = None,
) -> SandboxSession:
    from temporalio import activity

    key = workflow_run_id or activity.info().workflow_run_id
    cached = _SESSIONS.get(key)
    if cached is not None:
        return cached
    sandbox_name = ref.sandbox_id if ref is not None else _sandbox_name_for_run(key)
    if ref is not None and not is_local_backend(backend) and Sandbox is not None:
        try:
            session = SandboxSession(backend, local_project_root, sandbox_name=sandbox_name)
            session._microsandbox = await Sandbox.start(sandbox_name, detached=True)
            session._started = True
            session._paused = False
            _SESSIONS[key] = session
            return session
        except MicrosandboxError:
            pass
    session = SandboxSession(backend, local_project_root, sandbox_name=sandbox_name)
    _SESSIONS[key] = session
    return session


def run_tool_in_sandbox(
    user_fn: Callable[..., Awaitable[BaseModel]],
    local_project_root: Path,
    backend: AnyBackend,
) -> Callable[..., Awaitable[BaseModel]]:
    output_model_class: type[BaseModel] = inspect.get_annotations(user_fn, eval_str=True)["return"]

    async def wrapper(*args: Any, **kwargs: Any) -> BaseModel:
        if os.environ.get(_REMOTE_EXECUTION_MODE_ENV_VAR) == "1":
            return await user_fn(*args, **kwargs)

        if is_local_backend(backend) and len(args) == 1 and not kwargs:
            return await user_fn(args[0])

        arg = args[0]
        python_code = _EXECUTION_TEMPLATE.format(
            import_model=_get_import_path(type(arg), local_project_root),
            import_func=_get_import_path(user_fn, local_project_root),
            model_name=type(arg).__name__,
            func_name=user_fn.__name__,
            arg_json=repr(arg.model_dump_json()),
        )
        session = _current_session.get()
        if session is None:
            raise RuntimeError("run_tool_in_sandbox called outside an active SandboxSession")
        stdout_str = await session.run_code(python_code)
        return _parse_tool_response(output_model_class, stdout_str)

    return wrapper


async def ensure_snapshot_ready(backend: MicrosandboxBackend) -> None:
    if not backend.snapshot_name:
        return
    if Snapshot is None:
        raise RuntimeError(_INSTALL_MESSAGE)
    try:
        await Snapshot.open(backend.snapshot_name)
    except MicrosandboxError as exc:
        raise FileNotFoundError(
            f"Sandbox snapshot {backend.snapshot_name!r} not found: {exc}. "
            "Build it offline with build_sandbox(config)."
        ) from exc


_SESSIONS: dict[str, SandboxSession] = {}
