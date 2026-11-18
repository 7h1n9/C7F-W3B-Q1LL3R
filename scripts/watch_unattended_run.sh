#!/bin/bash
# Self-healing watchdog for an unattended arena run.
#
# Why this exists (measured 2026-11-09)
# -------------------------------------
# An overnight run was left going at 02:04 with 15 Challenges queued.  By 06:30 it
# had produced 5 Runs and 3 solve, and then nothing.  The cause was not the solver:
#
#   * ``docker ps -a`` showed every container ``Exited (255)`` -- Docker Desktop
#     itself had gone away around 03:50;
#   * the backend logged
#     ``sqlalchemy.exc.OperationalError (2003, "Can't connect to MySQL server on
#     '127.0.0.1'")``;
#   * the Run in flight died as ``FAILED_ENGINE / MUTEKI_RUNTIME_ERROR``;
#   * the queue kept 16 rows PENDING and never moved again.
#
# The polling script that was running (``scripts/_watch_queue.sh``) could not
# notice, because it reads the queue through ``mysql ... 2>/dev/null``: when MySQL
# is gone that returns an empty string, and an empty string looks exactly like "no
# change since last poll".  It sat there for three hours reporting nothing.
#
# So this script checks the *layer underneath* the queue:
#
#   1. is the MySQL container running?       -> ``docker start``
#   2. can it actually answer a query?       -> restart it
#   3. is the backend process listening?     -> relaunch it
#   4. is the queue stalled with work left?  -> nudge it
#
# Every check is explicit about failure.  A health probe that swallows stderr is
# how the previous one missed a three-hour outage.
#
# Usage:
#   bash scripts/watch_unattended_run.sh                 # until --until, default 07:00
#   nohup bash scripts/watch_unattended_run.sh --until 06:30 &
#
# Exit codes: 0 = finished or nothing left to do, 1 = could not recover.

set -uo pipefail

ROOT="/d/desktop/毕业设计/C7F-W3B-Q1LL3R"
cd "$ROOT" || { echo "cannot cd to $ROOT" >&2; exit 1; }

MYSQL_CONTAINER="c7f-w3b-q1ll3r-mysql-1"
BACKEND_PORT="${BACKEND_PORT_OVERRIDE:-8000}"
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
POLL_SECONDS="${POLL_SECONDS:-120}"
LOG="${LOG:-$ROOT/.probe/watchdog.log}"

UNTIL="07:00"
ONCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --until) UNTIL="$2"; shift 2 ;;
    --poll)  POLL_SECONDS="$2"; shift 2 ;;
    # ``--once``: run a single health-check-and-repair cycle and exit.
    #
    # This is the mode that actually survives.  Measured 2026-11-09/10: a
    # long-running background loop was killed by the host twice (once at 17:33
    # after ~4h, once at 00:30), each time leaving the queue stalled until a human
    # noticed.  The kills left no trace in the log -- no RECOVER, no FAIL, no
    # ``exit`` line -- which is how you tell "external kill" from "the script
    # decided to stop".
    #
    # A one-shot invocation has nothing for the host to reclaim.  Call it from a
    # short-lived loop, a scheduled task, or by hand; the repair logic is
    # idempotent either way.
    --once)  ONCE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$ROOT/.probe"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# ── probes ───────────────────────────────────────────────────────────────────
# Each returns 0 on success and says *why* on failure.  None of them redirect
# stderr to /dev/null: that is the mistake this script exists to correct.

mysql_alive() {
  # A real query, not `docker inspect`: a container can be Up while the server
  # inside refuses connections (or is still initialising after a start).
  docker exec "$MYSQL_CONTAINER" \
    mysql -uctf_agent -pctf_agent -N -B -e "select 1" ctf_agent 2>&1 | grep -qx "1"
}

backend_alive() {
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --noproxy '*' --max-time 5 \
          "$BACKEND_URL/api/v1/health" 2>&1)
  [ "$code" = "200" ]
}

q() {
  docker exec "$MYSQL_CONTAINER" \
    mysql -uctf_agent -pctf_agent -N -B -r -e "$1" ctf_agent 2>/dev/null
}

# ── repairs ──────────────────────────────────────────────────────────────────

ensure_docker() {
  # The daemon itself, not just the container.  `docker info` fails fast when the
  # engine is down, which is what happened overnight.
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  log "RECOVER: docker engine unreachable; attempting to start Docker Desktop"
  if [ -x "/c/Program Files/Docker/Docker/Docker Desktop.exe" ]; then
    start "" "/c/Program Files/Docker/Docker/Docker Desktop.exe" >/dev/null 2>&1 &
  fi
  for _ in $(seq 1 30); do
    sleep 10
    if docker info >/dev/null 2>&1; then
      log "RECOVER: docker engine is back"
      return 0
    fi
  done
  log "FAIL: docker engine still unreachable after 5 minutes"
  return 1
}

ensure_mysql() {
  if mysql_alive; then
    return 0
  fi
  local running
  running=$(docker inspect -f '{{.State.Running}}' "$MYSQL_CONTAINER" 2>/dev/null || echo "missing")
  if [ "$running" = "true" ]; then
    log "RECOVER: MySQL container is Up but not answering; restarting it"
    docker restart "$MYSQL_CONTAINER" >/dev/null 2>&1
  else
    log "RECOVER: MySQL container is '$running'; starting it"
    docker start "$MYSQL_CONTAINER" >/dev/null 2>&1
  fi
  for _ in $(seq 1 24); do
    sleep 5
    if mysql_alive; then
      log "RECOVER: MySQL is answering again"
      return 0
    fi
  done
  log "FAIL: MySQL still not answering after restart"
  return 1
}

ensure_backend() {
  if backend_alive; then
    return 0
  fi
  # The proxy state files pin the old (dead) proxy PIDs and stop the supervisor
  # from spawning new ones.  Clearing them is part of a known-good restart.
  log "RECOVER: backend not answering; restarting it"
  rm -f "$ROOT/.claude-proxy.json" "$ROOT/.codex-proxy.json"
  local pid
  pid=$(netstat -ano 2>/dev/null | grep ":${BACKEND_PORT} " | grep LISTENING | awk '{print $5}' | head -1)
  if [ -n "${pid:-}" ]; then
    MSYS_NO_PATHCONV=1 taskkill /PID "$pid" /F >/dev/null 2>&1
    sleep 3
  fi
  ( env -u PYTHONPATH BACKEND_PORT_OVERRIDE="$BACKEND_PORT" \
      bash "$ROOT/scripts/_run_backend.sh" > "$ROOT/.probe/backend.log" 2>&1 & )
  for _ in $(seq 1 24); do
    sleep 5
    if backend_alive; then
      log "RECOVER: backend is back"
      return 0
    fi
  done
  log "FAIL: backend did not come back after restart"
  return 1
}

nudge_queue() {
  # After an engine or DB recovery the scheduler can be idle with rows still
  # PENDING.  `start` is idempotent ("already_running" is a success).
  local out
  out=$(curl -s --noproxy '*' --max-time 20 -X POST "$BACKEND_URL/api/v1/arena/queue/start" 2>&1)
  log "RECOVER: queue start -> $(echo "$out" | head -c 120)"
}

# ── main loop ────────────────────────────────────────────────────────────────

log "watchdog start; until=$UNTIL poll=${POLL_SECONDS}s"

# ``--until`` is a wall-clock time, so the deadline is "the next time the clock
# reads $UNTIL", not "today at $UNTIL".  A run started at 23:00 with
# ``--until 06:30`` must survive midnight; comparing ``HH:MM`` strings alone would
# exit on the very first poll because 23:00 > 06:30.  Anchoring to an epoch
# deadline also makes the boundary unambiguous at exactly $UNTIL.
deadline_epoch() {
  local target="$1" now_epoch now_hm delta
  now_epoch=$(date +%s)
  now_hm=$(date +%H:%M)
  # Seconds from now until the next occurrence of HH:MM.
  delta=$(( (10#${target%%:*} * 3600 + 10#${target##*:} * 60)
          - (10#${now_hm%%:*} * 3600 + 10#${now_hm##*:} * 60) ))
  if [ "$delta" -le 0 ]; then
    delta=$(( delta + 86400 ))   # already passed today -> tomorrow
  fi
  echo $(( now_epoch + delta ))
}

DEADLINE=$(deadline_epoch "$UNTIL")

# One cycle: probe every layer, repair what is broken, report the queue.
# Returns 0 when the environment is healthy afterwards, 1 when it is not.
# Returns 2 when there is nothing left to do (queue drained).
#
# ``idle`` counts how many consecutive cycles saw work queued with nothing in
# flight.  In ``--once`` mode it is read from and written back to a state file so
# that three separate invocations still add up to "wedged"; otherwise a one-shot
# run could never satisfy a rule that needs history.
run_cycle() {
  if ! ensure_docker; then return 1; fi
  if ! ensure_mysql; then return 1; fi
  if ! ensure_backend; then return 1; fi

  pending=$(q "select count(*) from arena_solve_queue where status='PENDING';")
  running=$(q "select count(*) from arena_solve_queue where status='RUNNING';")
  solved=$(q "select count(*) from arena_solve_queue where status='SOLVED';")

  # An empty read here means the query itself failed, which should have been
  # caught by the probes above -- but never silently treat it as "nothing left".
  if [ -z "${pending:-}" ] || [ -z "${running:-}" ]; then
    log "WARN: queue counters unreadable; will re-probe"
    return 1
  fi

  log "queue: solved=$solved pending=$pending running=$running"

  if [ "$pending" = "0" ] && [ "$running" = "0" ]; then
    log "queue drained; nothing left to run"
    return 2
  fi

  # Work queued but nothing in flight for several consecutive cycles => the
  # scheduler is wedged (this is the state the overnight outage left behind).
  if [ "$pending" -gt 0 ] && [ "$running" = "0" ]; then
    idle=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
    idle=$(( idle + 1 ))
    echo "$idle" > "$STATE_FILE"
    if [ "$idle" -ge 3 ]; then
      log "WARN: $pending pending with nothing running for $idle cycles"
      nudge_queue
      echo 0 > "$STATE_FILE"
    fi
  else
    echo 0 > "$STATE_FILE"
  fi
  return 0
}

STATE_FILE="$ROOT/.probe/watchdog.idle"

if [ "$ONCE" = "1" ]; then
  log "watchdog --once"
  run_cycle
  rc=$?
  log "watchdog done (rc=$rc)"
  exit $rc
fi

log "watchdog start; until=$UNTIL poll=${POLL_SECONDS}s"
log "deadline: $(date -d "@$DEADLINE" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "$UNTIL (next occurrence)")"

while :; do
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    log "reached --until $UNTIL; stopping"
    break
  fi
  run_cycle
  case $? in
    1) exit 1 ;;
    2) break ;;
  esac
  sleep "$POLL_SECONDS"
done

log "watchdog exit"
