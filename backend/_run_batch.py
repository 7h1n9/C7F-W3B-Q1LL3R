"""Queue five BUU course challenges -- real CTF tasks with a single flag.

Deliberately excludes the range environments (sqli-labs, Juice Shop, DVWA,
Upload-Labs, Kali Linux, ...): those are platforms to practise on, not a single
challenge with a flag, so a batch of them can only ever come back UNSOLVED.
"""
import json, urllib.request, urllib.error

BASE = "http://127.0.0.1:8000/api/v1"
WANT = [
    "BUU XSS COURSE 1",
    "BUU UPLOAD COURSE 1",
    "BUU BURP COURSE 1",
    "BUU SQL COURSE 2",
    "BUU XXE COURSE 1",
]

def get(p, t=90):
    with urllib.request.urlopen(f"{BASE}{p}", timeout=t) as r:
        return json.load(r)

def post(p, body=None, t=90):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(f"{BASE}{p}", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=t) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

conn = get("/arena/connections")["data"][0]
grounds = get(f"/arena/grounds?connection_id={conn['id']}")["data"]
items = grounds.get("items") if isinstance(grounds, dict) else grounds
basic = next(g for g in items if g["name"] == "BUUCTF-Basic")
ch = get(f"/arena/mcp-challenges?connection_id={conn['id']}&practice_ground_id={basic['id']}&limit=100")["data"]

# First occurrence per name wins, so the duplicated catalogue rows do not double-queue.
by_name, seen = {}, set()
for x in ch["items"]:
    n = (x.get("name") or "").strip()
    if n not in by_name:
        by_name[n] = x

ids, names = [], []
for name in WANT:
    row = by_name.get(name)
    if row is None:
        print(f"  跳过（目录无此题）: {name}"); continue
    if row.get("is_solved"):
        print(f"  跳过（已解出）: {name}"); continue
    if not row.get("has_container"):
        print(f"  跳过（非容器题）: {name}"); continue
    ids.append(str(row["id"])); names.append(name)

print("本批:", ", ".join(names))
st, body = post("/arena/queue/enqueue-ids", {
    "practice_ground_id": basic["id"], "challenge_ids": ids, "priority": 0})
print("入队 →", st, json.dumps(body.get("data") or body, ensure_ascii=False))
