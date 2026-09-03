#!/usr/bin/env bash
#
# /usr/local/bin/supervise.sh
# Snapshot ENTRYPOINT for Daytona container sandboxes.
#
# Runs on EVERY sandbox boot — create, start, and start-from-archive — because
# it's the container's long-running entrypoint. It keeps the container alive and
# (re)launches your server, so when the proxy calls sandbox.start() the process
# comes back automatically. No agent action at runtime.
#
# Contract:
#   * The agent writes ONE launch command into $START_FILE.
#   * Optional env/secrets live in $ENV_FILE and are sourced before each launch.
#   * Both live INSIDE the project dir (/home/daytona/project) so the agent can create them with its
#     own `write` tool, which confines writes to the project root (must match tools.py's PROJECT_ROOT).
#   * All of this lives on the PERSISTENT filesystem, so it survives
#     stop -> start and archive -> start cycles unchanged.

set -u -o pipefail
# `set -m` puts each launched job in its own process group, so on shutdown we
# can signal the whole tree (server + anything it spawned), not just the shell.
set -m

# START_FILE/ENV_FILE live INSIDE the project dir so the agent can create them with its `write` tool
# (writes are confined to the project root). LOG stays OUTSIDE the project so the server's log doesn't
# pollute the agent's grep/glob/git of its own project (it can still `bash cat` it if it needs to).
START_FILE="${START_FILE:-/home/daytona/project/start.sh}"
ENV_FILE="${ENV_FILE:-/home/daytona/project/.env}"
LOG="${SERVER_LOG:-/home/daytona/server.log}"
MIN_BACKOFF=1
MAX_BACKOFF=30
HEALTHY_SECONDS=15   # server stayed up at least this long => reset backoff

log() {
  printf '[supervise %s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG" >&2
}

child_pgid=""

# As PID 1 we get NO default signal handlers. Without this trap, sandbox.stop()
# hangs until Daytona SIGKILLs us — slowing every stop/start the proxy drives.
# Forward the signal to the child's whole process group for a fast, clean stop.
shutdown() {
  log "shutdown signal received"
  if [ -n "$child_pgid" ]; then
    kill -TERM "-$child_pgid" 2>/dev/null || true
    for _ in $(seq 1 20); do            # up to ~5s grace
      kill -0 "-$child_pgid" 2>/dev/null || break
      sleep 0.25
    done
    kill -KILL "-$child_pgid" 2>/dev/null || true
  fi
  exit 0
}
trap shutdown TERM INT

# A fresh sandbox may boot before the agent has written the launch command.
log "supervisor started; waiting for $START_FILE"
while [ ! -s "$START_FILE" ]; do sleep 1; done
log "found launch command"

backoff=$MIN_BACKOFF
while true; do
  # Load env/secrets fresh on each launch (survives restarts, lives on disk).
  if [ -f "$ENV_FILE" ]; then
    set -a; . "$ENV_FILE"; set +a
  fi

  started_at=$(date +%s)
  log "launching server"
  bash "$START_FILE" >>"$LOG" 2>&1 &
  child_pid=$!
  child_pgid=$child_pid          # with `set -m`, the job's pgid == its pid

  wait "$child_pid"
  code=$?
  child_pgid=""
  ran=$(( $(date +%s) - started_at ))
  log "server exited (code=$code) after ${ran}s"

  # Reset backoff if it ran healthily; otherwise back off to avoid a hot loop.
  if [ "$ran" -ge "$HEALTHY_SECONDS" ]; then
    backoff=$MIN_BACKOFF
  else
    backoff=$(( backoff * 2 ))
    [ "$backoff" -gt "$MAX_BACKOFF" ] && backoff=$MAX_BACKOFF
  fi

  log "restarting in ${backoff}s"
  sleep "$backoff"
done
