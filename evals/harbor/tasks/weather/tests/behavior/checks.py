"""Weather-task assertions. Rewardkit discovers this file without renaming it."""

import json
from pathlib import Path

import rewardkit as rk
from rewardkit import criterion

# A built-in ATIF criterion; custom criteria below check this task's contract.
rk.trajectory_tool_used("get_weather", path="trajectory.json")


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
def correct_weather_arguments(workspace: Path) -> bool:
    trajectory = json.loads((workspace / "trajectory.json").read_text())
    calls = [call for step in trajectory["steps"] for call in (step.get("tool_calls") or [])]
    return bool(calls) and all(
        call["function_name"] == "get_weather"
        and isinstance(call["arguments"].get("city"), str)
        and call["arguments"]["city"].strip().casefold() == "paris"
        for call in calls
    )


@criterion
def successful_weather_result(workspace: Path) -> bool:
    trajectory = json.loads((workspace / "trajectory.json").read_text())
    calls = [call for step in trajectory["steps"] for call in (step.get("tool_calls") or [])]
    results = {
        result["source_call_id"]: result
        for step in trajectory["steps"]
        for result in (step.get("observation") or {}).get("results", [])
    }
    return bool(calls) and all(
        call["tool_call_id"] in results
        and not (results[call["tool_call_id"]].get("extra") or {}).get("error", False)
        and "72°F" in results[call["tool_call_id"]].get("content", "")
        and "sunny" in results[call["tool_call_id"]].get("content", "")
        for call in calls
    )
