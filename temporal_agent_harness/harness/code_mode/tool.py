"""The ``code_mode_tool`` factory: turn a set of harness tools into one run-a-script tool.

Code Mode lets a model accomplish a task by WRITING A SCRIPT that calls many tools with real
control flow — loops, conditionals, arithmetic, and ``asyncio.gather`` for concurrency —
instead of emitting one tool call at a time. :func:`code_mode_tool` takes the tools to expose
and returns a SINGLE inline tool, ``<name>(script: str) -> str``, that runs a model-authored
Python script in a sandbox whose only capabilities are those tools (surfaced as async host
functions). Every host call the script makes is dispatched through the runner, so it keeps that
tool's approval policy and tool_start/tool_end lifecycle.

Hand the returned tool to an agent's tool-calling loop exactly like any other tool. An agent may
hold several Code Mode tools at once (each with a distinct ``name``) over disjoint or overlapping
tool sets; each carries its own independent host-function surface.

``mounts`` give the sandbox a virtual filesystem the script reads and writes with ``open()`` and
``pathlib`` (see :mod:`.vfs`).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from pydantic import TypeAdapter
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_workflow import _current_runner, tool_defn

    from .driver import CodeModeDriver, call_monty, load_stepper
    from .stubs import render_host_interface, render_type_check_stubs, resolve_hints
    from .vfs import Mount, validate_mounts

# The model-facing contract. ``{sandbox}`` says what the sandbox can reach, ``{interface}`` is the
# generated host-function signatures (and their descriptions + result TypedDicts) for this tool's
# specific tool set, and ``{filesystem}`` lists its mounts, if it has any.
_CONTRACT = """\
Run a Python `script` in a sandbox and return its output. Accomplish tasks by WRITING CODE that \
calls the host functions listed below — using variables, loops, conditionals, comprehensions and \
arithmetic to combine many calls in one go — instead of calling tools one at a time.

{sandbox} The host functions and the types below are ALREADY IN SCOPE — never import them \
(there is no module to import them from). Calling a `TypedDict` type builds a plain dict, as in \
Python. The host functions are ASYNC — you MUST `await` them — so structure every \
script like this:

    import asyncio
    async def main():
        ...                 # await host functions here
        return <final value>
    asyncio.run(main())

The value of the script's LAST EXPRESSION becomes the result (along with anything you `print`). \
Run INDEPENDENT host calls CONCURRENTLY with `asyncio.gather(...)`; only await sequentially when \
a later call needs an earlier call's result. Host results come back as plain dicts/lists/scalars \
— index into them with normal Python (e.g. `results[0]["field"]`). A host function that fails \
RAISES at its `await`: catch it with `try`/`except Exception` to handle the failure, or let it \
propagate to end the script with an error.

Your script is STATICALLY TYPE-CHECKED against the host-function signatures below BEFORE it runs: \
a wrong argument type, or reading a result key that doesn't exist, comes back as an error to fix \
rather than a result. The signatures and result shapes below are exact — only the listed keys \
exist.

Host functions available:

{interface}
{filesystem}"""

_NO_FILESYSTEM = (
    "The sandbox has no filesystem, no network, and no imports except `asyncio` and the host "
    "functions below."
)

_WITH_FILESYSTEM = (
    "The sandbox has no network, and no imports except `asyncio`, `pathlib` and the host "
    "functions below. It has the filesystem listed under Filesystem below: read and write it "
    "with `open()` and `pathlib.Path`. File operations are synchronous (never `await` them) and "
    "work on whole files; read lines with `f.read().splitlines()`, not `for line in f`. A failed "
    "file operation raises an `OSError` subclass (`FileNotFoundError`, `FileExistsError`, "
    "`IsADirectoryError`, `NotADirectoryError`, `PermissionError`) whose message names the "
    "path; it has no `errno`."
)

# Declared for the type checker, which otherwise rejects these names as unknown. At run time
# they are the sandbox's own `open` and exception types.
_FILESYSTEM_STUB = """
import os as _os
import typing as _typing
def open(file: str | _os.PathLike[str], mode: str = "r", encoding: str | None = None) -> _typing.IO[_typing.Any]: ...
class FileNotFoundError(OSError): ...
class FileExistsError(OSError): ...
class IsADirectoryError(OSError): ...
class NotADirectoryError(OSError): ...
class PermissionError(OSError): ...
"""


def _render_filesystem(mounts: Sequence[Mount]) -> str:
    """The model-facing listing of the mounts."""
    if not mounts:
        return ""
    lines = ["\nFilesystem:\n"]
    for mount in mounts:
        access = "read-only" if mount.read_only else "writable"
        about = f": {mount.description}" if mount.description else ""
        lines.append(f"- `{mount.path}` ({access}){about}")
    return "\n".join(lines) + "\n"


def _validate_tools(
    tools: list[Callable[..., Awaitable[Any]]], *, has_mounts: bool
) -> dict[str, Callable[..., Awaitable[Any]]]:
    """Check the tool set and return a ``{name: tool}`` dispatch map, or raise ``ValueError``.

    Enforces, all at construction time: the set is non-empty, unless the tool has mounts to
    work on; every tool is a real harness tool (so its calls go through the approval policy);
    and no two tools share a name. Tools with ``Injected[...]`` parameters are fine — those are
    already hidden from the generated stubs and are supplied by the harness at dispatch, not by
    the script."""
    if not tools and not has_mounts:
        raise ValueError("code_mode_tool requires at least one tool or mount.")

    by_name: dict[str, Callable[..., Awaitable[Any]]] = {}
    for tool in tools:
        is_harness_tool = getattr(tool, "__agent_activity_tool__", False) or getattr(
            tool, "__agent_tool__", False
        )
        if not is_harness_tool:
            raise ValueError(
                f"code_mode_tool only accepts harness tools (declared with "
                f"@agent.activity_tool_defn or @agent.tool_defn), so every host call is subject "
                f"to the agent's approval policy. Got {tool!r}, which is not a harness tool."
            )
        name = tool.__name__
        if name in by_name:
            raise ValueError(
                f"duplicate host-function name {name!r} in this code_mode_tool: two tools would "
                f"collide as the same host function. Give them distinct names."
            )
        by_name[name] = tool
    return by_name


def _build_coercers(
    tools: list[Callable[..., Awaitable[Any]]],
) -> dict[str, dict[str, TypeAdapter[Any]]]:
    """A ``{tool_name: {param_name: TypeAdapter}}`` map used to coerce each sandbox-supplied
    argument into the tool's declared parameter type before dispatch. Built once here so the
    per-call driver does no adapter construction (and any un-adaptable type fails fast now)."""
    coercers: dict[str, dict[str, TypeAdapter[Any]]] = {}
    for tool in tools:
        hints = resolve_hints(tool)
        adapters: dict[str, TypeAdapter[Any]] = {}
        for pname, param in inspect.signature(tool).parameters.items():
            annotation = hints.get(pname, param.annotation)
            if annotation is not inspect.Parameter.empty:
                adapters[pname] = TypeAdapter(annotation)
        coercers[tool.__name__] = adapters
    return coercers


def code_mode_tool(
    tools: list[Callable[..., Awaitable[Any]]],
    *,
    name: str,
    inherently_safe: bool = True,
    auto_approval_criteria: str | None = None,
    injections: Mapping[str, Any] | None = None,
    mounts: Sequence[Mount] = (),
) -> Callable[..., Awaitable[str]]:
    """Expose ``tools`` to a model as ONE tool that runs a Python script calling them.

    Returns a single inline tool, ``<name>(script: str) -> str``. Declare it to your model like
    any other tool (``function_param(fn)``) and dispatch it via ``runner.run_tool``; its
    generated docstring tells the model the sandbox contract and every host function's signature +
    result shape. When the model calls it, the script runs in a sandbox and each host call it makes
    is dispatched through the runner to the matching tool — inheriting that tool's approval policy
    and tool lifecycle events.

    Args:
        tools: the harness tools to expose as host functions. Each must be declared with
            ``@agent.activity_tool_defn`` or ``@agent.tool_defn`` (checked), so every host call is
            subject to the agent's approval policy. A tool's ``Injected[...]`` parameters are
            hidden from the script (and from the generated stubs) and supplied from ``injections``
            at dispatch — the script only sees the model-facing parameters. Subagent toolsets from
            ``agent.subagent_toolset`` are valid tools, so Code Mode composes over subagents.
        name: the generated tool's name. Must be distinct from every other tool on the same
            agent — including other ``code_mode_tool``s — so the model can address each one.
        injections: values for the tools' ``Injected[...]`` parameters, keyed by parameter name.
            The harness supplies these to every host call (a tool takes only the injected names it
            declares); the script never provides them. Omit when no tool has injected parameters.
        inherently_safe: applies to THIS run-code tool only, NOT to the host calls it makes.
            Writing/compiling a script is inert — nothing happens until the script ``await``s a
            host function, and each host call re-enters the approval gate under its OWN tool's
            policy. Default ``True``: under a policy that auto-approves inherently-safe tools, the
            code-writing step is transparent while every real side effect still gates at the
            host-call layer. Set ``False`` to make the run-code tool a single human review
            checkpoint on the whole script before any host call runs. (Under a policy that gates
            everything, the run-code tool gates regardless.)
        auto_approval_criteria: the default criteria-set name auto mode judges THIS run-code
            tool against, if it is ever gated and auto mode is on. Like ``inherently_safe`` it
            applies to the run-code tool only, NOT to the host calls the script makes — each of
            those re-enters the gate under its own tool's declared set. Leave unset to fall back
            to whatever the operator configured as the catch-all.
        mounts: the sandbox's virtual filesystem, as :class:`~.vfs.Mount` objects at distinct,
            non-nested paths. Each file operation a script makes is carried out as ``fs_*`` tool
            calls through the runner, so it is subject to the approval policy and publishes
            tool lifecycle events. Build the mounts' backends in ``@agent.init``, so each
            workflow has its own.

    Raises:
        ValueError: the tool set is empty without mounts, contains a non-harness callable, or
            has a duplicate host-function name; or mounts overlap.
        RuntimeError: the worker lacks the ``code-mode`` extra. Raised at construction (at
            ``@agent.init``) so it fails the workflow task, which Temporal retries until a worker
            with the extra picks it up.
        CodeModeStubError: a tool's parameter or result type cannot be rendered into faithful
            type-check stubs (see :mod:`.stubs`).
    """
    mount_list = validate_mounts(mounts)
    tools_by_name = _validate_tools(tools, has_mounts=bool(mount_list))
    load_stepper()
    # Generated once at construction; raises now (not at script time) if a type is unrenderable.
    stubs = render_type_check_stubs(tools)
    if mount_list:
        stubs = replace(stubs, source=stubs.source + _FILESYSTEM_STUB)
    host_interface = render_host_interface(tools)
    coercers = _build_coercers(tools)
    docstring = _CONTRACT.format(
        sandbox=_WITH_FILESYSTEM if mount_list else _NO_FILESYSTEM,
        interface=host_interface if tools else "(none)",
        filesystem=_render_filesystem(mount_list),
    )
    injection_values = dict(injections or {})

    async def _run_code(script: str) -> str:
        driver = CodeModeDriver(
            _current_runner(),
            tools_by_name,
            coercers,
            injections=injection_values,
            stubs=stubs,
            mounts=mount_list,
        )
        return await driver.run_script(script)

    _run_code.__name__ = name
    _run_code.__qualname__ = name
    _run_code.__doc__ = docstring
    _run_code.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "script", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            )
        ],
        return_annotation=str,
    )
    _run_code.__annotations__ = {"script": str, "return": str}
    tool = tool_defn(
        inherently_safe=inherently_safe, auto_approval_criteria=auto_approval_criteria
    )(_run_code)
    # Read back by code_mode_type_check, so a script can be checked against these exact stubs.
    tool.__code_mode_stubs__ = stubs  # type: ignore[attr-defined]
    return tool


async def code_mode_type_check(code_tool: Callable[..., Awaitable[str]], script: str) -> str | None:
    """Type-check ``script`` against ``code_tool``'s host functions, without running it.

    ``code_tool`` is a tool :func:`code_mode_tool` returned. The script gets the same checks
    that tool gives it before every run — syntax, unknown host functions, wrong argument shapes
    and result keys that don't exist — and the script itself never executes, so no host call is
    made. Returns the checker's report, each error with its line, or ``None`` when the script is
    clean. Call it from workflow code or outside a workflow alike: the check is deterministic,
    so it runs in place.

    Use it to let an author (typically a model writing scripts for a user to run later) confirm a
    script will start before handing it over.
    """
    stubs = getattr(code_tool, "__code_mode_stubs__", None)
    if stubs is None:
        raise TypeError(f"{code_tool!r} is not a tool returned by code_mode_tool")
    return call_monty(load_stepper().type_check, script, stubs)
