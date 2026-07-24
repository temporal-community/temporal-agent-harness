"""transport — the uniform, in-workflow MCP transport (``WorkflowTransport``) that reaches
every Nexus-registered tool source (1st-party Nexus-native servers and, if one's registered,
a proxy like the Durable Tools Gateway) through ``workflow.create_nexus_client()``, never
real I/O directly in the workflow.
"""
