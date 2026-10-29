# ABOUTME: Skips this directory when temporal-nexus-mcp isn't importable.
#
# temporal-nexus-mcp is not published to PyPI — it resolves from nexus/mcp through a
# workspace-local [tool.uv.sources] path — so an environment built outside a checkout of this
# repo, or synced without the dev group, simply has no `nexus_mcp` module and every test module
# here fails at import. Skipping collection turns that into one explicit absence instead of a
# wall of ImportErrors.

from __future__ import annotations

from importlib.util import find_spec

collect_ignore_glob = [] if find_spec("nexus_mcp") is not None else ["*"]
