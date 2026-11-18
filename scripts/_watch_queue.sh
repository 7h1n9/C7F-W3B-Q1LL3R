#!/bin/bash
# Ephemeral agent helper: polls the arena solve queue until nothing is
# PENDING/RUNNING, then prints the final tally.  Read-only on the queue itself.
cd "/d/desktop/毕业设计/C7F-W3B-Q1LL3R" || exit 1
Q() {
  docker exec c7f-w3b-q1ll3r-mysql-1 mysql -uctf_agent -pctf_agent ctf_agent -N -e "$1" 2>/dev/null
}
prev=""
deadline=$(( $(date +%s) + 10800 ))
for i in $(seq 1 400); do
  summary=$(Q "SELECT CONCAT(challenge_name,'=',status,'/',COALESCE(verdict,'-')) FROM arena_solve_queue WHERE status IN ('PENDING','RUNNING') ORDER BY created_at;" | tr '\n' '|')
  if [ "$summary" != "$prev" ]; then
    solved=$(Q "SELECT COUNT(*) FROM arena_solve_queue WHERE verdict='accepted';")
    echo "[$(date +%H:%M:%S)] solved=$solved :: $summary"
    prev="$summary"
  fi
  active=$(Q "SELECT COUNT(*) FROM arena_solve_queue WHERE status IN ('PENDING','RUNNING');")
  if [ "$active" = "0" ]; then
    echo "ALL_DONE_AT=$(date +%H:%M:%S)"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "WATCHER_TIMEOUT_AT=$(date +%H:%M:%S)"
    break
  fi
  sleep 30
done
echo "=== FINAL ==="
Q "SELECT status, COALESCE(verdict,'-') AS verdict, COUNT(*) FROM arena_solve_queue GROUP BY status, verdict;"
Q "SELECT challenge_name FROM arena_solve_queue WHERE verdict='accepted' ORDER BY updated_at;"
