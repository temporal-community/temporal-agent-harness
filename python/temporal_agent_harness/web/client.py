"""Temporal connection plumbing shared by the packaged server and worker entrypoints.

Both processes have to reach the *same* Temporal namespace with the *same* data converter, or
the server can't read what the workers wrote. In the bundled examples this logic was duplicated
in two launchers that could drift apart; here it lives once, so ``serve`` and
``session-manager`` resolve their connection identically by construction.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from temporalio.client import Client


# Matches the address `temporal server start-dev` listens on, and the Temporal CLI's own default.
DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"


def ensure_temporal_address(explicit_address: str | None) -> None:
    """Make sure this process's Temporal client has a target host to connect to.

    Both entrypoints resolve their connection via ``ClientConfig.load_client_connect_config()``
    (a ``temporal.toml`` profile plus ``TEMPORAL_*`` env overrides). With neither present,
    ``target_host`` is simply absent and the failure is a bare ``TypeError`` from
    ``Client.connect()`` — a rotten first run for someone who just
    added the harness to a project.
    So: an explicit ``--temporal-address`` always wins (env overrides file, as it should), and
    otherwise the dev-server default fills in only when the resolved config offers no host at
    all. An existing profile is never overridden.
    """
    if explicit_address is not None:
        os.environ["TEMPORAL_ADDRESS"] = explicit_address
        return

    from temporalio.envconfig import ClientConfig

    try:
        resolved = dict(ClientConfig.load_client_connect_config())
    except Exception:
        # A malformed/unreadable config is the caller's error to report, with its real message —
        # don't mask it here by guessing an address.
        return

    if not resolved.get("target_host"):
        os.environ["TEMPORAL_ADDRESS"] = DEFAULT_TEMPORAL_ADDRESS


async def connect_client() -> "Client":
    """Connect a Temporal client carrying the harness plugin.

    The plugin is where the shared data converter is DEFINED, so the session-manager worker, the
    web server, and every agent worker can read each other's payloads only because they all add
    it. Its default large-payload storage matches ``create_agent_harness_app``'s, keeping the
    packaged entrypoints mutually readable out of the box.
    """
    from temporalio.client import Client
    from temporalio.envconfig import ClientConfig

    from temporal_agent_harness.plugin import AgentHarnessPlugin

    connect_config = ClientConfig.load_client_connect_config()
    try:
        return await Client.connect(**connect_config, plugins=[AgentHarnessPlugin()])
    except RuntimeError as exc:
        # "no server running" is the commonest way a first run fails, and the SDK reports it as a
        # dozen frames of tonic/bridge internals. Translate just that failure into the two things
        # the reader needs — which address was tried, and how to change it — keeping the original
        # message as `detail:` so nothing is hidden. Any other RuntimeError propagates untouched
        # rather than being dressed up as a config problem.
        if "Failed client connect" not in str(exc):
            raise
        target = connect_config.get("target_host", "<none resolved>")
        raise SystemExit(
            f"error: cannot reach a Temporal server at {target}.\n"
            "  Start one with `temporal server start-dev`, or point this command at a\n"
            "  different cluster with --temporal-address HOST:PORT, TEMPORAL_ADDRESS, or a\n"
            "  temporal.toml profile (see --help for the full resolution order).\n"
            f"  detail: {exc}"
        ) from exc


async def verify_temporal_reachable() -> None:
    """Connect once and drop it, so connection problems are reported before a process starts."""
    await connect_client()
