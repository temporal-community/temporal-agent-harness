# ABOUTME: Skips the Nexus-brokered MCP test when temporal-nexus-mcp isn't importable.
#
# Same reason as tests/nexus_mcp/conftest.py — an environment without the unpublished
# temporal-nexus-mcp distribution — scoped to the one module that imports `nexus_mcp` at
# module level. Everything else in this directory must still be collected; see the two
# @requires_nexus_mcp tests in test_harness_mcp_server.py for the per-test form of the
# same guard, used where a module imports fine and only individual tests need the package.

from __future__ import annotations

from importlib.util import find_spec

collect_ignore = [] if find_spec("nexus_mcp") is not None else ["test_nexus_mcp.py"]
