#!/usr/bin/env bash
# 一键前置自检 / 拉起。判据见 docs/2026-11-18-plan.md。
#
# 用法:
#   bash scripts/preflight.sh                 只检查，不改任何状态
#   bash scripts/preflight.sh --start         缺什么起什么（Docker -> MySQL -> 后端 -> 前端）
#   bash scripts/preflight.sh --start --no-frontend
#
# 退出码: 0 全绿 / 1 有失败项
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

START=0
WITH_FRONTEND=1
for arg in "$@"; do
  case "$arg" in
    --start) START=1 ;;
    --no-frontend) WITH_FRONTEND=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

export MSYS_NO_PATHCONV=1
API="http://127.0.0.1:8000/api/v1"
MYSQL="c7f-w3b-q1ll3r-mysql-1"
PY="$ROOT/.venv3.11/Scripts/python.exe"
LOGDIR="$ROOT/.probe"
mkdir -p "$LOGDIR"

ROWS=()
row() { ROWS+=("$1|$2|$3"); }   # 状态|检查项|说明

http_code() { curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time "${2:-4}" "$1" 2>/dev/null; }

docker_up() { docker info >/dev/null 2>&1; }

mysql_status() { docker ps --format '{{.Status}}' --filter "name=$MYSQL" 2>/dev/null | head -1; }

wait_for() {   # wait_for <秒上限> <命令...>
  local limit="$1"; shift
  local i=0
  while [ "$i" -lt "$limit" ]; do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 3; i=$((i + 3))
  done
  return 1
}

echo "== 前置自检 ($(date '+%Y-%m-%d %H:%M')) =="

# ---------- 1. Docker ----------
if docker_up; then
  row OK "Docker 引擎" "$(docker info 2>/dev/null | grep -m1 'Server Version' | tr -s ' ')"
elif [ "$START" = 1 ]; then
  echo "  .. 启动 Docker Desktop ..."
  docker desktop start >"$LOGDIR/docker-desktop.log" 2>&1
  if wait_for 200 docker info; then
    row OK "Docker 引擎" "本次已启动"
  else
    row FAIL "Docker 引擎" "docker desktop start 后仍不可用，见 .probe/docker-desktop.log"
  fi
else
  row FAIL "Docker 引擎" "未运行；用 --start 或手动 docker desktop start"
fi

# ---------- 2. MySQL ----------
if ! docker_up; then
  row SKIP "MySQL 容器" "依赖 Docker"
else
  st="$(mysql_status)"
  if [ -z "$st" ]; then
    if [ "$START" = 1 ]; then
      echo "  .. 创建/启动 MySQL 容器 ..."
      docker start "$MYSQL" >/dev/null 2>&1 || docker compose up -d mysql >"$LOGDIR/mysql-up.log" 2>&1
      st="$(mysql_status)"
    fi
  fi
  case "$st" in
    *healthy*) row OK "MySQL 容器" "$st" ;;
    *Up*)      row WARN "MySQL 容器" "$st（尚未 healthy，稍等再查）" ;;
    *)
      if [ "$START" = 1 ] && [ -n "$st" ]; then
        row WARN "MySQL 容器" "$st"
      else
        row FAIL "MySQL 容器" "未运行；用 --start"
      fi
      ;;
  esac

  # 端口可达性（真连一次，避免只看容器状态）
  if docker exec "$MYSQL" mysql -uctf_agent -pctf_agent -N -B -r -e \
      "select 1" ctf_agent >/dev/null 2>&1; then
    row OK "MySQL 可查询" "ctf_agent 库可读"
  else
    row FAIL "MySQL 可查询" "容器在但查询失败（端口 3307 / 凭据）"
  fi
fi

# ---------- 3. 后端 ----------
code="$(http_code "$API/health")"
if [ "$code" = "200" ]; then
  row OK "后端 8000" "health 200"
elif [ "$START" = 1 ]; then
  echo "  .. 启动后端 ..."
  rm -f "$ROOT/.claude-proxy.json" "$ROOT/.codex-proxy.json"
  ( cd "$ROOT" && env -u PYTHONPATH BACKEND_PORT_OVERRIDE=8000 \
      bash scripts/_run_backend.sh >"$LOGDIR/backend.log" 2>&1 & )
  if wait_for 120 bash -c "[ \"$(http_code "$API/health")\" = 200 ]"; then
    row OK "后端 8000" "本次已启动"
  else
    row FAIL "后端 8000" "启动超时，见 .probe/backend.log"
  fi
else
  row FAIL "后端 8000" "health=${code:-000}；用 --start"
fi

# ---------- 4. 前端 ----------
if [ "$WITH_FRONTEND" = 1 ]; then
  code="$(http_code "http://127.0.0.1:5173/")"
  if [ "$code" = "200" ]; then
    row OK "前端 5173" "200"
  elif [ "$START" = 1 ]; then
    echo "  .. 启动前端 ..."
    ( cd "$ROOT/frontend" && VITE_API_BASE_URL="$API" \
        npm run dev -- --host 127.0.0.1 --port 5173 >"$LOGDIR/frontend.log" 2>&1 & )
    if wait_for 120 bash -c "[ \"$(http_code http://127.0.0.1:5173/)\" = 200 ]"; then
      row OK "前端 5173" "本次已启动"
    else
      row FAIL "前端 5173" "启动超时，见 .probe/frontend.log"
    fi
  else
    row FAIL "前端 5173" "http=${code:-000}；用 --start"
  fi
else
  row SKIP "前端 5173" "--no-frontend"
fi

# ---------- 5. 竞技场连接 / 环境 / 队列 ----------
if [ "$(http_code "$API/health")" = "200" ]; then
  read -r CONN_ID ENVS ENV_OK QLINE SCHED < <("$PY" - <<'PY'
import json, os, re, urllib.request
for k in ("http_proxy","https_proxy","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","all_proxy"):
    os.environ.pop(k, None)
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(p):
    try:
        return json.load(op.open("http://127.0.0.1:8000/api/v1" + p, timeout=15))
    except Exception:
        return {}
# /arena/connections 的 data 是 list
conns = (get("/arena/connections") or {}).get("data") or []
conn_id = str(conns[0].get("id") or "") if conns else ""
st = (get("/arena/status") or {}).get("data") or {}
# quota:<name> 的 message 形如 "environments 0/1; read 60/min, write 30/min"
envs, env_ok = "-", "-"
for c in st.get("checks", []):
    m = re.search(r"environments\s+(\d+)\s*/\s*(\d+)", str(c.get("message", "")))
    if m:
        envs = "%s/%s" % (m.group(1), m.group(2))
        env_ok = "yes" if m.group(1) == "0" else "no"
        break
q = st.get("queue_counts") or {}
ql = " ".join("%s=%s" % (k, v) for k, v in q.items())
sched = ""
for c in st.get("checks", []):
    if c.get("name") == "scheduler":
        sched = str(c.get("message", ""))[:80]
print(conn_id or "-", envs, env_ok, ql or "-", sched or "-")
PY
)

  if [ "$CONN_ID" = "-" ]; then
    row FAIL "竞技场连接" "拿不到连接列表"
  else
    row OK "竞技场连接" "${CONN_ID:0:8}"
    if [ "$START" = 1 ]; then
      "$PY" - "$CONN_ID" <<'PY' >/dev/null 2>&1
import json, os, sys, urllib.request
for k in ("http_proxy","https_proxy","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","all_proxy"):
    os.environ.pop(k, None)
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
cid = sys.argv[1]
req = urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/arena/orphans/reclaim?connection_id=" + cid,
    method="POST")
op.open(req, timeout=60).read()
PY
      row OK "环境回收" "已请求 reclaim（active_environments 见下方状态）"
    else
      row WARN "环境回收" "未执行；开跑前必须 reclaim 并确认 active_environments: 0"
    fi
    row INFO "队列" "$QLINE"
    row INFO "调度器" "$SCHED"
    if [ "$ENV_OK" = "yes" ]; then
      row OK "环境占用" "active_environments ${ENVS}（可开跑）"
    elif [ "$ENV_OK" = "no" ]; then
      row WARN "环境占用" "active_environments ${ENVS}（非 0，开跑前必须 reclaim）"
    else
      row WARN "环境占用" "未从 status 读到配额"
    fi
  fi
else
  row SKIP "竞技场连接/队列" "后端未就绪"
fi

# ---------- 6. 工程基线提醒 ----------
if [ -f "$ROOT/backend/tests/conftest.py" ]; then
  row INFO "测试基线" "2486 passed（另 10 个需 MySQL；跑给合并门用）"
fi

# ---------- 输出 ----------
echo
printf '  %-6s %-18s %s\n' "状态" "检查项" "说明"
printf '  %-6s %-18s %s\n' "------" "------------------" "----"
for r in "${ROWS[@]}"; do
  IFS='|' read -r s n m <<<"$r"
  printf '  %-6s %-18s %s\n' "$s" "$n" "$m"
done

FAILED=$(printf '%s\n' "${ROWS[@]}" | grep -c '^FAIL|' || true)
echo
if [ "${FAILED:-0}" -eq 0 ]; then
  echo "结果: 全部就绪，可以开始。"
  exit 0
else
  echo "结果: ${FAILED} 项未就绪。用 bash scripts/preflight.sh --start 自动拉起。"
  exit 1
fi
