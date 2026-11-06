import os

from temporalio.envconfig import ClientConfig

from harness_evals.demo import worker
from harness_evals.demo_entry import fix_openai_tool_events


def test_local_demo_does_not_inherit_cloud_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setenv("TEMPORAL_API_KEY", "unrelated-cloud-placeholder")
    monkeypatch.setenv("TEMPORAL_PROFILE", "unrelated-profile")
    repo = tmp_path / "harness"
    (repo / "python/examples").mkdir(parents=True)
    captured = {}
    monkeypatch.setattr(os, "execvpe", lambda executable, args, env: captured.update(env))
    worker("openai", "127.0.0.1:7323", "default", repo)
    assert "TEMPORAL_API_KEY" not in captured
    assert captured["TEMPORAL_PROFILE"] == "harness_eval"
    config = ClientConfig.load_client_connect_config(override_env_vars=captured)
    assert config["target_host"] == "127.0.0.1:7323"
    assert config["tls"] is False
    assert "api_key" not in config


def test_openai_done_event_uses_opening_call_metadata(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from temporal_agent_harness.ai_sdks.openai_agents_harness import OpenAIStreamObserver

    original = OpenAIStreamObserver._emit_tool_requested
    monkeypatch.setattr(OpenAIStreamObserver, "_emit_tool_requested", original)
    fix_openai_tool_events()
    observer = SimpleNamespace(_fn_calls={"item1": ("call1", "get_weather")}, _arg_buffers={})
    publisher = SimpleNamespace(publish=Mock())
    event = SimpleNamespace(item_id="item1", arguments='{"city":"Paris"}')
    OpenAIStreamObserver._emit_tool_requested(observer, event, publisher)
    result = publisher.publish.call_args.args[0]
    assert result.tool_id == "call1"
    assert result.tool_name == "get_weather"
    assert result.tool_input == {"city": "Paris"}
