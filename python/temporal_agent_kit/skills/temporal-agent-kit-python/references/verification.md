# Verification and troubleshooting

Validate against the installed SDK version. Run the starter's schema command before launching
services. A successful check includes discovering the agent, creating a session, sending a typed
message, seeing a tool result and completed reply, and reconnecting to the same session.
Use a scripted model for repeatable automated tests; do not claim a scripted response is a live
provider test. Test approval/callback paths separately when your agent uses them.

If the console loads but cannot start a session, check the session-manager worker. If a session
exists but messages stall, check the agent worker, task queue, and namespace. If a model fails,
check the worker's provider credentials and optional integration dependencies. If registration
changes appear stale, inspect the session manager's cached registry before replacing local state.

Keep the Temporal server, namespace, and task queues isolated for tests. Do not terminate unrelated
sessions or reset a user's existing development server to make a test pass. Temporal workflow code
must remain deterministic; move external I/O into activities. Replaying a completed activity uses
its recorded result, but retryable activities with side effects still need idempotent design.
