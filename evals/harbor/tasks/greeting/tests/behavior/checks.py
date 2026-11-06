"""Greeting-task assertions, alongside the task's test.sh and judge rubric."""

import json
from pathlib import Path

import rewardkit as rk
from rewardkit import criterion

rk.trajectory_tool_not_used("get_weather", path="trajectory.json")


@criterion
def completed(workspace: Path) -> bool:
    record = json.loads((workspace / "run.json").read_text())
    trajectory = json.loads((workspace / "trajectory.json").read_text())
    return (
        record.get("complete") is True
        and (trajectory.get("extra") or {}).get("complete") is True
        and record.get("output") is not None
        and record["session_id"] == trajectory["session_id"]
    )


@criterion
def no_tools(workspace: Path) -> bool:
    trajectory = json.loads((workspace / "trajectory.json").read_text())
    return not any(step.get("tool_calls") for step in trajectory["steps"])
