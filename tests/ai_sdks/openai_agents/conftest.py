# ABOUTME: Skips the Nexus-brokered MCP test when temporal-nexus-mcp isn't importable.
#
# Same reason as tests/nexus_mcp/conftest.py, but scoped to the one module that needs
# `nexus_mcp`: test_stream_observer.py alongside it must still run on every interpreter.

from __future__ import annotations

from importlib.util import find_spec

collect_ignore = [] if find_spec("nexus_mcp") is not None else ["test_nexus_mcp.py"]
