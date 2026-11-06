import copy
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import rewardkit

from harness_evals.atif import export_atif
from harness_evals.cli import rescore

ROOT = Path(__file__).resolve().parents[1]


def save_recording(path, record, trajectory=None):
    path.mkdir()
    raw = path / "run.json"
    raw.write_text(json.dumps(record))
    (path / "trajectory.json").write_text(
        json.dumps(trajectory or export_atif(record).to_json_dict())
    )
    return raw


def arguments(recording, task, output):
    return Namespace(
        recording=recording,
        task=task,
        output=output,
        judge=False,
        judge_model="openai/gpt-5.1",
    )


@pytest.mark.parametrize(
    "trial", ["weather__sxfq4Wh", "weather__DK8K2bi", "greeting__8bFA9jc", "greeting__B8C4dsp"]
)
def test_same_task_entrypoint_regrades_saved_examples_without_credentials(
    tmp_path, monkeypatch, trial
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    recording = ROOT / "examples/saved-jobs/rewardkit-matrix" / trial / "agent/run.json"
    evidence = [recording, recording.with_name("trajectory.json")]
    original = [path.read_bytes() for path in evidence]
    output = tmp_path / "scores"
    assert rescore(arguments(recording, ROOT / "tasks" / trial.split("__")[0], output)) == 0
    assert json.loads((output / "reward.json").read_text()) == {"reward": 1}
    assert json.loads((output / "reward-details.json").read_text())["reward"]["criteria"]
    assert [path.read_bytes() for path in evidence] == original


@pytest.mark.parametrize("corruption", ["wrong_city", "tool_error", "missing_result", "incomplete"])
def test_task_checks_fail_real_behavior_defects(tmp_path, weather_record, corruption):
    trajectory = export_atif(weather_record).to_json_dict()
    tool_step = trajectory["steps"][1]
    if corruption == "wrong_city":
        tool_step["tool_calls"][0]["arguments"]["city"] = "Tokyo"
    elif corruption == "tool_error":
        tool_step["observation"]["results"][0]["extra"]["error"] = True
    elif corruption == "missing_result":
        tool_step["observation"] = None
    else:
        weather_record["complete"] = False
        weather_record["output"] = None
        trajectory["extra"]["complete"] = False
    raw = save_recording(tmp_path / "recording", weather_record, trajectory)
    output = tmp_path / "scores"
    assert rescore(arguments(raw, ROOT / "tasks/weather", output)) == 1
    assert json.loads((output / "reward.json").read_text())["reward"] == 0


def test_greeting_rejects_weather_tool_use(tmp_path, weather_record):
    raw = save_recording(tmp_path / "recording", weather_record)
    assert rescore(arguments(raw, ROOT / "tasks/greeting", tmp_path / "scores")) == 1


def test_optional_atif_null_fields_are_valid_input(tmp_path, weather_record):
    trajectory = json.loads(export_atif(weather_record).model_dump_json())
    raw = save_recording(tmp_path / "recording", weather_record, trajectory)
    assert rescore(arguments(raw, ROOT / "tasks/weather", tmp_path / "scores")) == 0


def test_rescore_refuses_existing_output_before_running_any_grader(tmp_path, monkeypatch):
    output = tmp_path / "existing"
    output.mkdir()
    execute = Mock()
    monkeypatch.setattr("harness_evals.cli.subprocess.run", execute)
    with pytest.raises(FileExistsError):
        rescore(arguments(tmp_path / "missing", ROOT / "tasks/weather", output))
    execute.assert_not_called()


def test_rescore_rejects_mismatched_sessions(tmp_path, weather_record):
    trajectory = export_atif(weather_record).to_json_dict()
    trajectory["session_id"] = "different-session"
    raw = save_recording(tmp_path / "recording", weather_record, trajectory)
    with pytest.raises(ValueError, match="different sessions"):
        rescore(arguments(raw, ROOT / "tasks/weather", tmp_path / "scores"))


def test_rewardkit_discovers_judge_rubric_and_preserves_reasoning(
    tmp_path, monkeypatch, weather_record
):
    recording = tmp_path / "recording"
    save_recording(recording, weather_record)
    monkeypatch.chdir(recording)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setenv("REWARDKIT_JUDGE", "openai/gpt-5.1")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "score": "yes",
                            "reasoning": "The answer matches the tool result in step 2.",
                        }
                    )
                )
            )
        ],
        usage=None,
    )
    complete = AsyncMock(return_value=response)
    monkeypatch.setattr("rewardkit.judges.litellm.acompletion", complete)
    monkeypatch.setattr("rewardkit.judges.litellm.encode", lambda **kwargs: [])
    output = tmp_path / "scores/reward.json"
    rewards = rewardkit.run(ROOT / "tasks/weather/tests", workspace=recording, output=output)
    assert rewards == {"behavior": 1, "quality": 1, "reward": 1}
    request = complete.call_args.kwargs
    assert request["model"] == "openai/gpt-5.1"
    assert "Paris" in json.dumps(request["messages"])
    assert "get_weather" in json.dumps(request["messages"])
    details = json.loads(output.with_name("reward-details.json").read_text())
    assert details["quality"]["criteria"][0]["reasoning"].endswith("step 2.")


def test_rewardkit_judge_failure_cannot_leave_a_passing_reward(
    tmp_path, monkeypatch, weather_record
):
    recording = tmp_path / "recording"
    save_recording(recording, copy.deepcopy(weather_record))
    monkeypatch.chdir(recording)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setattr(
        "rewardkit.judges.litellm.acompletion",
        AsyncMock(side_effect=RuntimeError("judge unavailable")),
    )
    monkeypatch.setattr("rewardkit.judges.litellm.encode", lambda **kwargs: [])
    output = tmp_path / "scores/reward.json"
    with pytest.raises(ExceptionGroup):
        rewardkit.run(ROOT / "tasks/weather/tests", workspace=recording, output=output)
    assert not output.exists()
