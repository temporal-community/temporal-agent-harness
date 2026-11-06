import copy

import pytest
from harbor.models.trajectories.trajectory import Trajectory

from harness_evals.atif import UnsupportedRecording, export_atif


def test_exports_model_steps_and_tool_results_without_duplicate_calls_or_reply(weather_record):
    trajectory = export_atif(weather_record)
    Trajectory.model_validate_json(trajectory.model_dump_json())
    assert len(trajectory.steps) == 3
    assert trajectory.steps[0].message == "Weather in Paris?\n"
    assert len(trajectory.steps[1].tool_calls) == 1
    result = trajectory.steps[1].observation.results[0]
    assert result.source_call_id == trajectory.steps[1].tool_calls[0].tool_call_id
    assert result.content == "It's 72°F and sunny in Paris."
    assert trajectory.steps[2].message == weather_record["output"]["text"]
    assert trajectory.final_metrics.total_prompt_tokens == 220
    assert trajectory.final_metrics.total_completion_tokens == 22
    assert trajectory.final_metrics.total_cached_tokens is None


def test_missing_token_usage_stays_unknown(weather_record):
    del weather_record["events"][10]["event"]["usage"]
    assert export_atif(weather_record).final_metrics.total_prompt_tokens is None


def test_incomplete_recording_preserves_partial_trace(weather_record):
    weather_record["events"] = weather_record["events"][:4]
    weather_record["complete"] = False
    weather_record["output"] = None
    data = export_atif(weather_record).to_json_dict()
    assert data["extra"]["complete"] is False
    assert data["steps"][1]["tool_calls"][0]["function_name"] == "get_weather"


@pytest.mark.parametrize(
    "corruption", ["missing_result", "second_agent", "overlap", "second_message"]
)
def test_rejects_incomplete_or_uncorrelatable_evidence(weather_record, corruption):
    if corruption == "missing_result":
        weather_record["events"] = [
            e for e in weather_record["events"] if e["event"]["type"] != "tool_end"
        ]
    elif corruption == "second_agent":
        weather_record["events"][-1]["agent_id"] = "child1"
    elif corruption == "second_message":
        weather_record["events"][-2]["message_id"] = "message2"
    else:
        weather_record["events"].insert(3, copy.deepcopy(weather_record["events"][2]))
    with pytest.raises(UnsupportedRecording):
        export_atif(weather_record)


def test_parallel_same_name_tools_keep_distinct_results(weather_record):
    events = weather_record["events"]
    second = copy.deepcopy(events[3])
    second["event"].update(tool_id="call2", tool_input={"city": "Tokyo"})
    events.insert(4, second)
    result = copy.deepcopy(events[7])
    result["event"].update(tool_id="call2", tool_output="It's 72°F and sunny in Tokyo.")
    events.insert(8, result)
    step = export_atif(weather_record).steps[1]
    assert {c.tool_call_id for c in step.tool_calls} == {"call1", "call2"}
    assert {r.source_call_id: r.content for r in step.observation.results} == {
        "call1": "It's 72°F and sunny in Paris.",
        "call2": "It's 72°F and sunny in Tokyo.",
    }
