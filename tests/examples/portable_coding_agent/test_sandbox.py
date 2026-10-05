"""Tests for sandbox backend selection (examples.portable_coding_agent.sandbox).

These check the routing (env -> backend type, and options matching the backend)
without creating a container, so they run anywhere. The live sandbox execution is
exercised by hand (see the example README).
"""

import os

import pytest

from examples.portable_coding_agent import sandbox as sb


@pytest.fixture(autouse=True)
def _restore_env():
    prev = {k: os.environ.get(k) for k in ("CODING_AGENT_SANDBOX", "CODING_AGENT_SANDBOX_IMAGE")}
    try:
        yield
    finally:
        for k, v in prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_default_kind_is_docker():
    os.environ.pop("CODING_AGENT_SANDBOX", None)
    assert sb.sandbox_kind() == "docker"


def test_local_backend_builds_without_docker():
    os.environ["CODING_AGENT_SANDBOX"] = "local"
    client = sb.build_sandbox_client()
    assert type(client).__name__ == "UnixLocalSandboxClient"
    assert type(sb.sandbox_options()).__name__ == "UnixLocalSandboxClientOptions"


def test_docker_options_carry_the_image():
    os.environ["CODING_AGENT_SANDBOX"] = "docker"
    os.environ["CODING_AGENT_SANDBOX_IMAGE"] = "python:3.12-slim"
    opts = sb.sandbox_options()
    assert type(opts).__name__ == "DockerSandboxClientOptions"
    assert opts.image == "python:3.12-slim"


def test_unknown_kind_is_rejected():
    os.environ["CODING_AGENT_SANDBOX"] = "vm"
    with pytest.raises(ValueError, match="docker.*local"):
        sb.build_sandbox_client()
    with pytest.raises(ValueError, match="docker.*local"):
        sb.sandbox_options()
