# ABOUTME: Skips this directory when temporal-nexus-mcp isn't importable.
#
# The dependency is declared `temporal-nexus-mcp; python_version >= '3.13'` — that package's own
# floor — so on 3.11/3.12 the `nexus_mcp` module is simply absent and every test module here
# fails at import. The package supports 3.11+, so `uv run pytest` has to be green on 3.11 and
# 3.12 too; skipping collection is what makes the marker's consequence explicit instead of a
# wall of ImportErrors.

from __future__ import annotations

from importlib.util import find_spec

collect_ignore_glob = [] if find_spec("nexus_mcp") is not None else ["*"]
