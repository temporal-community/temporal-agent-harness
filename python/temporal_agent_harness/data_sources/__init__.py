"""Ready-made data sources (see :mod:`temporal_agent_harness.harness.data_source`).

Each one talks to its service with the standard library, and reads its credential from the
worker's environment inside the activity."""

from temporal_agent_harness.data_sources.jira import JiraDataSource
from temporal_agent_harness.data_sources.notion import NotionDataSource

__all__ = ["JiraDataSource", "NotionDataSource"]
