"""Tests for the packaged ``temporal-agent-harness`` console script.

The CLI's whole job is to make the prebuilt UI runnable from a plain ``pip install``, so what
matters here is (a) that it never overrides a Temporal profile the user already configured, and
(b) that the connection rules are actually discoverable from the command itself.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from temporal_agent_harness.web.cli import main
from temporal_agent_harness.web.client import (
    DEFAULT_TEMPORAL_ADDRESS,
    connect_client,
    ensure_temporal_address,
)


@pytest.fixture(autouse=True)
def isolated_temporal_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Neutralize the developer's own Temporal config so these tests are hermetic.

    Pointing TEMPORAL_CONFIG_FILE at a nonexistent path disables file loading without error,
    which is exactly the "nothing configured" starting state each test then builds on.
    """
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    monkeypatch.delenv("TEMPORAL_PROFILE", raising=False)
    monkeypatch.setenv("TEMPORAL_CONFIG_FILE", str(tmp_path / "absent.toml"))


def _write_profile(path, address: str) -> None:
    path.write_text(
        "[profile]\n"
        "  [profile.default]\n"
        f'    address = "{address}"\n'
        '    namespace = "cfg-namespace"\n'
    )


def test_configured_profile_is_not_overridden(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A temporal.toml profile must survive untouched — no TEMPORAL_ADDRESS written over it."""
    config = tmp_path / "temporal.toml"
    _write_profile(config, "cfg-host:1234")
    monkeypatch.setenv("TEMPORAL_CONFIG_FILE", str(config))

    ensure_temporal_address(None)

    from temporalio.envconfig import ClientConfig

    resolved = dict(ClientConfig.load_client_connect_config())
    assert resolved["target_host"] == "cfg-host:1234"
    assert resolved["namespace"] == "cfg-namespace"
    # The fallback must not have fired at all.
    assert "TEMPORAL_ADDRESS" not in __import__("os").environ


def test_existing_env_address_is_not_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "env-host:5555")

    ensure_temporal_address(None)

    import os

    assert os.environ["TEMPORAL_ADDRESS"] == "env-host:5555"


def test_explicit_address_wins_over_configured_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``--temporal-address`` overrides the address but leaves the rest of the profile alone."""
    config = tmp_path / "temporal.toml"
    _write_profile(config, "cfg-host:1234")
    monkeypatch.setenv("TEMPORAL_CONFIG_FILE", str(config))

    ensure_temporal_address("flag-host:9999")

    from temporalio.envconfig import ClientConfig

    resolved = dict(ClientConfig.load_client_connect_config())
    assert resolved["target_host"] == "flag-host:9999"
    assert resolved["namespace"] == "cfg-namespace"


def test_dev_server_default_applies_only_when_nothing_is_configured() -> None:
    ensure_temporal_address(None)

    import os

    assert os.environ["TEMPORAL_ADDRESS"] == DEFAULT_TEMPORAL_ADDRESS


@pytest.mark.parametrize(
    "argv",
    [["--help"], [], ["serve", "--help"], ["session-manager", "--help"]],
)
def test_connection_rules_are_discoverable_from_the_command(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Every way in must explain how the Temporal address resolves.

    Including each subcommand's own ``--help``: someone reading `serve --help` shouldn't have to
    back out to the top level to learn which temporal.toml it will read.
    """
    with pytest.raises(SystemExit) as exit_info:
        main(argv)

    # --help exits 0; a bare invocation is an incomplete call, so it still exits non-zero.
    assert exit_info.value.code == (0 if argv else 2)

    out = capsys.readouterr().out
    assert "Temporal connection:" in out
    assert "--temporal-address" in out
    assert DEFAULT_TEMPORAL_ADDRESS in out
    assert "TEMPORAL_CONFIG_FILE" in out
    # Profile selection is the other half of file-based config, so it must be shown too —
    # worked through in an example, not just named in the precedence list.
    assert "TEMPORAL_PROFILE" in out
    assert "TEMPORAL_CONFIG_FILE=./temporal.toml TEMPORAL_PROFILE=cloud" in out
    # The surprising part specifically: a cwd temporal.toml is not auto-discovered.
    assert "CURRENT DIRECTORY is NOT picked up" in out
    assert "agents.toml" in out

def test_bare_invocation_lists_both_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """The session-manager worker must be discoverable as a command, not a file to hand-write."""
    with pytest.raises(SystemExit):
        main([])

    out = capsys.readouterr().out
    assert "serve" in out
    assert "session-manager" in out


def test_unknown_subcommand_names_the_valid_choices(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--temporal-address belongs to the subcommands, so a bare flag reads as a bad command."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--temporal-address", "somewhere:7233"])

    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "serve" in err
    assert "session-manager" in err


def test_session_manager_defaults_to_the_packaged_task_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`session-manager` with no flags must poll the queue the server actually expects."""
    from temporal_agent_harness.web.session_manager import SESSION_MANAGER_TASK_QUEUE

    captured: dict[str, object] = {}

    def fake_run(*, task_queue: str, identity: str | None) -> None:
        captured["task_queue"] = task_queue
        captured["identity"] = identity

    monkeypatch.setattr(
        "temporal_agent_harness.web.session_manager_worker.run_session_manager_worker", fake_run
    )

    main(["session-manager"])

    assert captured == {"task_queue": SESSION_MANAGER_TASK_QUEUE, "identity": None}


def test_session_manager_forwards_task_queue_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*, task_queue: str, identity: str | None) -> None:
        captured["task_queue"] = task_queue
        captured["identity"] = identity

    monkeypatch.setattr(
        "temporal_agent_harness.web.session_manager_worker.run_session_manager_worker", fake_run
    )

    main(["session-manager", "--task-queue", "custom-queue", "--identity", "worker-7"])

    assert captured == {"task_queue": "custom-queue", "identity": "worker-7"}


def test_session_manager_honors_the_temporal_address_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection flag has to work on BOTH subcommands, not just serve."""

    def fake_run(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "temporal_agent_harness.web.session_manager_worker.run_session_manager_worker", fake_run
    )

    main(["session-manager", "--temporal-address", "manager-host:7233"])

    assert os.environ["TEMPORAL_ADDRESS"] == "manager-host:7233"


async def _raise_connect_failure(*_args: object, **_kwargs: object):
    raise RuntimeError(
        'Failed client connect: Server connection error: ConnectError("tcp connect error", '
        "127.0.0.1:7233, ConnectionRefused)"
    )


async def _raise_unrelated_failure(*_args: object, **_kwargs: object):
    raise RuntimeError("something else entirely went wrong")


def test_unreachable_server_reports_the_address_and_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead server is the commonest first-run failure; it must not be a 12-frame traceback."""
    import temporalio.client

    monkeypatch.setenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    monkeypatch.setattr(temporalio.client.Client, "connect", _raise_connect_failure)

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(connect_client())

    message = str(exit_info.value)
    assert "127.0.0.1:7233" in message
    assert "temporal server start-dev" in message
    assert "--temporal-address" in message
    # The original error stays visible rather than being swallowed.
    assert "detail: Failed client connect" in message


def test_unrelated_connect_errors_are_not_dressed_up_as_config_problems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import temporalio.client

    monkeypatch.setenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    monkeypatch.setattr(temporalio.client.Client, "connect", _raise_unrelated_failure)

    with pytest.raises(RuntimeError, match="something else entirely"):
        asyncio.run(connect_client())


def test_serve_checks_temporal_before_binding_the_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The reachability check must run BEFORE uvicorn takes the port, not during ASGI startup."""
    registry = tmp_path / "agents.toml"
    registry.write_text(
        "[[agents]]\n"
        'key = "a"\n'
        'workflow_type = "A"\n'
        'task_queue = "q"\n'
        'label = "A"\n'
        'description = "d"\n'
    )

    calls: list[str] = []

    async def fake_verify() -> None:
        calls.append("verify")

    monkeypatch.setattr("temporal_agent_harness.web.serve.verify_temporal_reachable", fake_verify)
    monkeypatch.setattr("temporal_agent_harness.web.serve.create_app", lambda *_: "app")
    monkeypatch.setitem(
        __import__("sys").modules,
        "uvicorn",
        type("_Uvicorn", (), {"run": staticmethod(lambda *a, **k: calls.append("uvicorn"))}),
    )

    main(["serve", str(registry)])

    assert calls == ["verify", "uvicorn"]
