"""Run Codex CLI as the inner agent loop of a Temporal harness workflow.

Install/authenticate Codex separately. Register ``CodexPlugin(CodexConfig(...))``
on the Temporal client, then call ``workflow.run_codex`` inside a harness turn.
The plugin supplies its activity and native harness event translation.
"""

from ._models import CodexConfig, CodexResult

__all__ = ["CodexConfig", "CodexPlugin", "CodexResult"]


def __getattr__(name: str):
    # Workflow imports need only contracts; subprocess setup stays on the worker.
    if name == "CodexPlugin":
        from ._plugin import CodexPlugin
        return CodexPlugin
    raise AttributeError(name)
