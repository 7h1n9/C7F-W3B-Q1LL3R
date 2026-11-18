import sqlite3
con = sqlite3.connect(r"backend\local-dev.db")
con.row_factory = sqlite3.Row
cur = con.cursor()
tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table' order by name")]
print("== TABLES ==")
print(", ".join(tables))
PREFIX = "8ddbdb34"
for t in tables:
    cols = [c[1] for c in cur.execute("pragma table_info(%s)" % t)]
    id_like = [c for c in cols if "id" in c.lower() or "uuid" in c.lower() or "group" in c.lower()]
    for c in id_like:
        try:
            rows = cur.execute("select * from %s where cast(%s as text) like ? limit 10" % (t, c), (PREFIX + "%",)).fetchall()
        except Exception as exc:
            continue
        if rows:
            print("== HIT %s.%s (%d) ==" % (t, c, len(rows)))
            for r in rows:
                d = dict(r)
                for k, v in d.items():
                    s = str(v)
                    print("  %s: %s%s" % (k, s[:200], "..." if len(s) > 200 else ""))
                print("  --")
