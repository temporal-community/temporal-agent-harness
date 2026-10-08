"""OpenAI's ``Shell`` and ``Filesystem`` capabilities, with tools tuned for long-running agents.

Drop-in replacements: the same capability types, configuration and ``configure_tools`` hook.
Only OpenAI's own bundled tools are swapped, so a tool ``configure_tools`` put in their
place is kept as it is.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import Any

from agents.sandbox.capabilities import filesystem, shell
from agents.sandbox.capabilities.tools import (
    ExecCommandTool,
    SandboxApplyPatchTool,
    WriteStdinTool,
)
from agents.sandbox.capabilities.tools.shell_tool import ExecCommandArgs, WriteStdinArgs
from agents.tool import FunctionTool, Tool
from pydantic import BaseModel, Field
from temporalio import workflow

# Shell output is cut inside the sandbox, so a huge output never leaves it. A command's output
# goes to a log under ``SPILL_DIR`` (outside the workspace, so snapshots don't carry it), and
# what comes back is the whole log when it fits in ``OUTPUT_BYTES``, else its head and a larger
# tail (errors and summaries come last) with the log's path for the model to search. The model's
# ``max_output_tokens`` (about 4 bytes each) replaces the budget.
OUTPUT_BYTES = 12_000
SPILL_DIR = PurePosixPath("/tmp/tool-output")

# POSIX sh, run by the shell OpenAI's tool starts, with ``cd <workdir> &&`` in front. The
# subshell keeps an ``exit`` in the command from skipping the summary, and the wrapper exits
# with the command's code, so the reported exit code is the command's. Python buffers output to
# a file, which would leave a running process's log empty, so it is told not to.
_BOUNDED_COMMAND = r"""{{
mkdir -p {spill_dir}
f={log}
( export PYTHONUNBUFFERED=1; eval {command} ) > "$f" 2>&1
code=$?
esc=$(printf '\033')
sed "s/$esc\[[0-9;?]*[A-Za-z]//g" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
size=$(($(wc -c < "$f")))
if [ "$size" -le {limit} ]; then
  cat "$f"
  rm -f "$f"
else
  echo "[$(($(wc -l < "$f"))) lines, $size bytes, cut to the first {head} and last {tail} bytes. \
All of it is in $f: search it with grep -n or read parts with sed -n, instead of running the \
command again.]"
  head -c {head} "$f"
  printf '\n[... %d bytes omitted ...]\n' $((size - {head} - {tail}))
  tail -c {tail} "$f"
fi
exit $code
}}"""

_ADD_FILE = "*** Add File: "
_END_PATCH = "*** End Patch"
_SECTION_ENDS = (_ADD_FILE, "*** Delete File: ", "*** Update File: ", _END_PATCH)

# OpenAI offers apply_patch as a freeform (grammar) tool; here it is a function taking the
# patch text, so the one sentence about the freeform format is reworded.
_FREEFORM_SENTENCE = "This is a FREEFORM tool, so do not wrap the patch in JSON."


class Shell(shell.Shell):
    """OpenAI's ``Shell``, whose ``exec_command`` keeps large output out of the conversation.

    A command without a TTY runs with its output in a log under ``SPILL_DIR``. What comes
    back is the whole output when it fits in ``OUTPUT_BYTES`` (or the model's
    ``max_output_tokens``), else its head and tail and the log's path to search. A command
    still running when the call returns names its log, to ``tail`` while it runs.
    ``write_stdin``, and a ``tty=true`` command, are capped at ``OUTPUT_BYTES`` by default.
    """

    def tools(self) -> list[Tool]:
        tools: list[Tool] = []
        for tool in super().tools():
            if type(tool) is ExecCommandTool:
                tool = BoundedExecCommandTool(
                    session=tool.session,
                    user=tool.user,
                    workspace_scope=tool.workspace_scope,
                    needs_approval=tool.needs_approval,
                )
            elif type(tool) is WriteStdinTool:
                tool = CappedWriteStdinTool(session=tool.session, needs_approval=tool.needs_approval)
            tools.append(tool)
        return tools


class Filesystem(filesystem.Filesystem):
    """OpenAI's ``Filesystem``, whose ``apply_patch`` is a function tool taking ``patch``
    and repairs two ``*** Add File`` mistakes models repeat (see :func:`repair_patch`)."""

    def tools(self) -> list[Tool]:
        return [
            _patch_function(tool) if type(tool) is SandboxApplyPatchTool else tool
            for tool in super().tools()
        ]


class BoundedExecCommandTool(ExecCommandTool):
    """``exec_command``, with a non-TTY command's output cut inside the sandbox."""

    async def run(self, args: ExecCommandArgs) -> str:
        if args.tty:
            # An interactive program needs the terminal itself, so its output can't go to a
            # log; OpenAI's own cap, applied on the worker, bounds what reaches the model.
            max_tokens = args.max_output_tokens or OUTPUT_BYTES // 4
            return await super().run(args.model_copy(update={"max_output_tokens": max_tokens}))
        log = SPILL_DIR / f"{workflow.uuid4().hex[:12]}.log"
        limit = args.max_output_tokens * 4 if args.max_output_tokens else OUTPUT_BYTES
        bounded = _BOUNDED_COMMAND.format(
            spill_dir=shlex.quote(str(SPILL_DIR)),
            log=shlex.quote(str(log)),
            command=shlex.quote(args.cmd),
            limit=limit,
            head=limit // 3,
            tail=limit // 2,
        )
        response = await super().run(
            args.model_copy(update={"cmd": bounded, "max_output_tokens": None})
        )
        if "Process running with session ID" in response:
            response += (
                f"\n[Its output goes to {log}; read it with tail or grep while it runs. A"
                " write_stdin check after it exits returns the output.]"
            )
        return response


class CappedWriteStdinTool(WriteStdinTool):
    """``write_stdin``, capped at ``OUTPUT_BYTES`` unless the model asks for another cap. A
    session ``exec_command`` started without a TTY prints only its bounded summary; this
    cap is for interactive ones."""

    async def run(self, args: WriteStdinArgs) -> str:
        max_tokens = args.max_output_tokens or OUTPUT_BYTES // 4
        return await super().run(args.model_copy(update={"max_output_tokens": max_tokens}))


class _ApplyPatchArgs(BaseModel):
    patch: str = Field(
        description=(
            "The patch, from `*** Begin Patch` to `*** End Patch`. Those two marker lines are "
            "never prefixed with `+`, even after an `*** Add File` section."
        )
    )


def _patch_function(tool: SandboxApplyPatchTool) -> FunctionTool:
    async def invoke(context: Any, raw: str) -> str:
        patch = _ApplyPatchArgs.model_validate_json(raw).patch
        return await tool.on_invoke_tool(context, repair_patch(patch))

    return FunctionTool(
        name=tool.name,
        description=tool.description.replace(_FREEFORM_SENTENCE, "Pass the whole patch text as `patch`."),
        params_json_schema=_ApplyPatchArgs.model_json_schema(),
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


def repair_patch(patch: str) -> str:
    """Fix two mistakes models make in ``*** Add File`` sections, whose intent is clear but
    which OpenAI's parser rejects. Told about them, models (Gemini especially) tend to resend
    the same patch, so they are repaired instead.

    - The closing marker prefixed with ``+`` like the file's lines: ``+*** End Patch``.
    - A section with no lines, meaning an empty file (an ``__init__.py``): it gets one empty
      ``+`` line, which creates an empty file.
    """
    lines = patch.splitlines()
    if lines and lines[-1] != _END_PATCH and lines[-1].lstrip("+ ") == _END_PATCH:
        lines[-1] = _END_PATCH
    repaired: list[str] = []
    for line, following in zip(lines, [*lines[1:], _END_PATCH], strict=True):
        repaired.append(line)
        if line.startswith(_ADD_FILE) and following.startswith(_SECTION_ENDS):
            repaired.append("+")
    return "\n".join(repaired)
