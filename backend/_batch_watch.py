"""Wait for the batch to drain, then summarise.

Polls until no item is PENDING or RUNNING, or until the budget runs out, so the
caller is notified once the batch is actually done rather than guessing at an
interval.
"""
import json, sys, time, urllib.request

BASE = "http://127.0.0.1:8000/api/v1"
BUDGET_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 3600

def get(p, t=90):
    with urllib.request.urlopen(f"{BASE}{p}", timeout=t) as r:
        return json.load(r)

start = time.time()
last = ""
while time.time() - start < BUDGET_SECONDS:
    q = get("/arena/queue")["data"]
    counts = q.get("counts") or {}
    active = int(counts.get("PENDING", 0)) + int(counts.get("RUNNING", 0))
    line = f"[{int(time.time()-start):>5}s] {counts}"
    if line != last:
        print(line, flush=True)
        last = line
    if active == 0:
        print("\n=== 批次结束 ===", flush=True)
        break
    time.sleep(30)

print("\n=== 最终结果 ===", flush=True)
q = get("/arena/queue")["data"]
for it in q["items"]:
    err = (it.get("last_error") or "")[:60]
    print(f"  {it['status']:<9} att={it['attempt_count']} {it['challenge_name'][:30]:<31} {err}", flush=True)
