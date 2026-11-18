import asyncio, traceback, sys, os
sys.path.insert(0, os.path.abspath("backend"))
os.chdir("backend")
import asyncmy
from app.core.config import get_settings
from app.services.crypto import decrypt_api_key
from app.engines.openai_compatible import OpenAICompatibleEngine

CFG_ID = "daf2b404-43aa-4dc1-9d62-7a8344adbe2b"

async def main():
    s = get_settings()
    url = s.database_url
    from urllib.parse import urlparse
    u = urlparse(url.replace("mysql+asyncmy", "mysql"))
    conn = await asyncmy.connect(host=u.hostname, port=u.port or 3306, user=u.username,
                                 password=u.password, db=u.path.lstrip("/"))
    async with conn.cursor() as cur:
        await cur.execute("select base_url, model_name, encrypted_api_key, request_timeout_seconds from model_configs where id=%s", (CFG_ID,))
        base_url, model, enc_key, cfg_timeout = await cur.fetchone()
    conn.close()
    key = decrypt_api_key(enc_key)
    timeout = max(120.0, float(cfg_timeout or 30))
    engine = OpenAICompatibleEngine(base_url, key, model, timeout=timeout, max_retries=2, retry_base_seconds=1.0)
    try:
        try:
            r = await engine.chat(model=model, messages=[{"role":"system","content":"You are a health probe. Reply with OK."},{"role":"user","content":"health"}], temperature=0.0, max_tokens=16, stream=False)
            print("HEALTH OK:", (r.content or "")[:80], "| trace:", engine.last_trace)
        except Exception as e:
            print("HEALTH FAIL:", type(e).__name__, str(e)[:500])
        try:
            from muteki.solver.reason import run_reason
            summary = __import__("json").dumps({"challenge_id":"probe","revision":180,"facts":[{"id":i,"content":"GET /path%d 200 nginx; param id=%d observed; reflection none" % (i,i)} for i in range(40)],"intents":[{"id":i,"goal":"probe route %d" % i,"status":"open"} for i in range(40)],"dead_ends":[{"id":i,"description":"route exhausted at depth %d" % i} for i in range(30)],"verified_flag_count":0}, ensure_ascii=False)
            res = await run_reason(llm=engine, model=model, graph_summary=summary, fact_index="", max_intents=4, mode="ctf")
            print("REASON OK:", str(res)[:300])
        except Exception as e:
            print("REASON FAIL:", type(e).__name__, str(e)[:500])
            traceback.print_exc(limit=3)
    finally:
        await engine.close()

asyncio.run(main())



