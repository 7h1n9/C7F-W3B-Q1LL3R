"""List BUUCTF-Basic container challenges with solved state, to pick a batch."""
import json, urllib.request

BASE = "http://127.0.0.1:8000/api/v1"

def get(p, t=90):
    with urllib.request.urlopen(f"{BASE}{p}", timeout=t) as r:
        return json.load(r)

conn = get("/arena/connections")["data"][0]
grounds = get(f"/arena/grounds?connection_id={conn['id']}")["data"]
items = grounds.get("items") if isinstance(grounds, dict) else grounds
basic = next(g for g in items if g["name"] == "BUUCTF-Basic")
ch = get(f"/arena/mcp-challenges?connection_id={conn['id']}&practice_ground_id={basic['id']}&limit=100")["data"]

solved = [x for x in ch["items"] if x.get("is_solved")]
cont = [x for x in ch["items"] if x.get("has_container")]
todo = [x for x in cont if not x.get("is_solved")]
print(f"总 {len(ch['items'])} | 容器 {len(cont)} | 已解 {len(solved)} | 待解容器 {len(todo)}")
print()
print("已解出:")
for x in solved:
    print(f"  ✅ {x['friendly_id']:<16} {(x.get('name') or '')[:40]}")
print()
print("待解容器题（按目录顺序）:")
for x in todo:
    print(f"  {x['friendly_id']:<16} {(x.get('difficulty') or '?'):<9} {(x.get('name') or '')[:42]}")
