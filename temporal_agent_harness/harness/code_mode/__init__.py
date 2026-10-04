"""Code Mode: expose a set of harness tools to a model as a single run-a-script tool.

``code_mode_tool(tools, name=...)`` returns one tool that runs a model-authored Python script in
a sandbox whose only capabilities are those tools (as async host functions). The model writes
code — with loops, conditionals, and ``asyncio.gather`` concurrency — to orchestrate many tool
calls in one turn; every host call goes through the runner, keeping its approval policy and tool
lifecycle events.

Scripts run inside the workflow: the workflow steps the sandbox itself, and only the host calls
are activities, so Code Mode needs no activities of its own (see :mod:`.monty_stepper`).

``mounts=`` gives scripts a virtual filesystem: ``Mount``\\ s of ``FileSystem`` backends such as
``InMemoryFileSystem``, read and written with ``open()`` and ``pathlib`` (see :mod:`.vfs`).

Importing the package never requires the sandbox engine (the optional ``code-mode`` extra):

  * :mod:`.stubs`, :mod:`.vfs`, :mod:`.driver` and :mod:`.tool` are safe to import anywhere,
    including inside a workflow. ``code_mode_tool``, ``code_mode_type_check``,
    ``CodeModeStubError`` and the filesystem types are the public surface, re-exported here.
  * :mod:`.monty_stepper` is the stepping engine and the only module that imports
    ``pydantic_monty``. ``code_mode_tool`` loads it, through the workflow sandbox's
    pass-through, when it builds a tool.
"""

from .stubs import CodeModeStubError
from .tool import code_mode_tool, code_mode_type_check
from .vfs import FileEntry, FileStat, FileSystem, FileTree, InMemoryFileSystem, Mount

__all__ = [
    "CodeModeStubError",
    "FileEntry",
    "FileStat",
    "FileSystem",
    "FileTree",
    "InMemoryFileSystem",
    "Mount",
    "code_mode_tool",
    "code_mode_type_check",
]
