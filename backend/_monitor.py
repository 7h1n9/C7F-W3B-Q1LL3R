"""Report batch progress once. Runs repeatedly from the shell between checks."""
import json, sys, urllib.request

BASE = "http://127.0.0.1:8000/api/v1"
def get(p, t=90):
    with urllib.request.urlopen(f"{BASE}{p}", timeout=t) as r:
        return json.load(r)

q = get("/arena/queue")["data"]
print("counts:", q.get("counts"), "| scheduler:", q.get("scheduler"))
for it in q["items"]:
    err = (it.get("last_error") or "")[:56]
    print(f"  {it['status']:<9} att={it['attempt_count']} {it['challenge_name'][:30]:<31} {err}")
