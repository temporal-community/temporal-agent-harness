"""The activity that loads the agent's skills from the ``skills/`` directory shipped with it.

The agent seeds its ``/skills`` mount from this activity's result. The files are read here, in
an activity, rather than in workflow code: the result is recorded in history, so a running
agent keeps the skills it started with even after a deploy changes the directory, and a replay
on any worker sees the same files. (``/memory``'s activities come from ``LocalDisk``; see
:mod:`.local_disk`.)
"""

from __future__ import annotations

from pathlib import Path

from temporalio import activity

SKILLS_DIR = Path(__file__).parent / "skills"


@activity.defn
async def load_skills() -> dict[str, str]:
    """Every file under ``skills/``, keyed by its path relative to that directory."""
    return {
        path.relative_to(SKILLS_DIR).as_posix(): path.read_text()
        for path in sorted(SKILLS_DIR.rglob("*"))
        if path.is_file()
    }
