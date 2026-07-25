"""Repair challenge names whose original dump already contains replacement '?'."""

import pymysql


REPAIRS = {
    "2874668b-6df7-4993-aaf6-cd16318e1974": "网址",
    "42da88a3-f133-4400-a526-98483fbba6ae": "文件路径穿越",
    "8d689f3f-4db0-4df4-add0-49a6ac7c2cfc": "弱口令后门",
    "a4c9b8d4-783d-4f88-9c14-ede08ac0cd7c": "内部物资搜索平台",
    "de086580-e17a-4f38-be24-630163b929ee": "敏感信息与备份文件泄露",
    "e5ff22a1-87ca-403d-af40-549634c2748d": "简单的sql注入",
    "f1b9d759-f391-4795-8687-b9a8d404bfc5": "你真的需要这个吗",
    "fc010bf4-e3f7-403f-a007-67404e03f27d": "弱口令后门",
}

conn = pymysql.connect(
    host="127.0.0.1", port=3307, user="ctf_agent", password="ctf_agent",
    database="ctf_agent", charset="utf8mb4", autocommit=True,
)
try:
    with conn.cursor() as cur:
        for challenge_id, name in REPAIRS.items():
            cur.execute("UPDATE challenges SET name=%s WHERE id=%s", (name, challenge_id))
            print(challenge_id, "->", name, "rows=", cur.rowcount)
finally:
    conn.close()
