"""General-purpose Temporal utilities used by the harness and examples but not part of the
agent harness itself.

  * :mod:`temporal_agent_harness.utils.large_payload` — claim-check offloading of oversized
    payloads to external storage (local filesystem or S3), applied at ``Client.connect``.
"""
