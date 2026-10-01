# Shared fixtures for the Playpen tests. The tests that run Dogwood need its CLI, which is a Rust
# binary built from github.com/dogwood-policy/dogwood (see examples/playpen/README.md); they are
# skipped, saying so, when DOGWOOD_CLI does not point at one.

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
POLICY_STORE = REPO / "examples" / "playpen" / "policies"


@pytest.fixture
def dogwood_cli() -> str:
    cli = os.environ.get("DOGWOOD_CLI")
    if not cli:
        pytest.skip("DOGWOOD_CLI is not set; see examples/playpen/README.md to build the CLI")
    if not Path(cli).is_file():
        pytest.fail(f"DOGWOOD_CLI={cli} does not exist")
    return cli
