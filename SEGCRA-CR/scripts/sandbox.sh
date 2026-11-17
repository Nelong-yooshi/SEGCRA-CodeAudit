#!/usr/bin/env bash
# 執行驗證(spec_exec)用的 SQL Server 沙盒。
#
# 規則程式跑在 MS SQL 上,執行驗證就必須在同一種引擎上跑 —— 不同引擎在型別轉換、
# 日期運算、NULL 處理上的細節不一樣,換引擎驗出來的「通過」不能代表正式環境。
#
# 這是一次性的沙盒:沒有掛載資料卷,容器刪掉資料就沒了;只綁 127.0.0.1,不對外。
# SQL Server 常駐約吃 2GB 記憶體,手動開關,用完請 stop。
#
#   scripts/sandbox.sh start | stop | status
#
# 密碼從 config/sandbox.env 讀(不進版控,範本見 config/sandbox.env.example)。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SANDBOX_ENV_FILE:-$HERE/../config/sandbox.env}"

# 讀密碼檔前先檢查權限,太寬就停(跟 orchestrator/config.py:check_config_file 同一套規則):
# 檔案群組或其他人可讀、可寫,或所在目錄群組或其他人可寫(可以把檔案整個換掉)。只看權限
# 位元,不看擁有者(掛進容器時擁有者是主機的 uid)。
check_secret_file() {
  local f="$1" d mode
  d="$(dirname "$f")"
  mode="$(stat -c %a "$f")"
  if [ $(( 8#$mode & 8#077 )) -ne 0 ]; then
    echo "檔案(含密碼)$f 的權限太寬($mode):群組或其他帳號讀得到或改得到,不往下跑。請執行:chmod 600 $f" >&2
    exit 1
  fi
  mode="$(stat -c %a "$d")"
  if [ $(( 8#$mode & 8#022 )) -ne 0 ]; then
    echo "目錄 $d 的權限太寬($mode):群組或其他帳號改得到,不往下跑。請執行:chmod 755 $d" >&2
    exit 1
  fi
}

if [ -f "$ENV_FILE" ]; then check_secret_file "$ENV_FILE"; set -a; . "$ENV_FILE"; set +a; fi

NAME="${SANDBOX_MSSQL_CONTAINER:-segcra-sandbox}"
IMAGE="${SANDBOX_MSSQL_IMAGE:-mcr.microsoft.com/mssql/server:2022-CU25-ubuntu-22.04}"
PORT="${SANDBOX_MSSQL_PORT:-1433}"

ready() {
  # 映像版本不同,sqlcmd 的路徑不同(新版在 mssql-tools18,需 -C 信任自簽憑證)
  docker exec "$NAME" bash -c '
    for c in /opt/mssql-tools18/bin/sqlcmd /opt/mssql-tools/bin/sqlcmd; do
      [ -x "$c" ] && exec "$c" -C -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -Q "SELECT 1" -b -l 3
    done; exit 1' >/dev/null 2>&1
}

case "${1:-status}" in
  start)
    : "${SANDBOX_MSSQL_PASSWORD:?未設定 SANDBOX_MSSQL_PASSWORD(見 config/sandbox.env.example)}"
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      echo "沙盒已經在跑"; exec "$0" status
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    # 密碼以「只給變數名」的方式傳進容器,不會出現在指令列或 ps 輸出裡
    MSSQL_SA_PASSWORD="$SANDBOX_MSSQL_PASSWORD" docker run -d --name "$NAME" \
      -p "127.0.0.1:${PORT}:1433" \
      -e ACCEPT_EULA=Y -e MSSQL_PID=Developer -e MSSQL_SA_PASSWORD \
      -e MSSQL_MEMORY_LIMIT_MB=2048 --memory 3g \
      --restart no "$IMAGE" >/dev/null
    echo "啟動中(第一次約需 20–40 秒)..."
    for _ in $(seq 1 60); do
      if ready; then echo "沙盒就緒:127.0.0.1:${PORT}"; exit 0; fi
      sleep 2
    done
    echo "120 秒內沒就緒,看 log:docker logs $NAME" >&2; exit 1
    ;;
  stop)
    docker rm -f "$NAME" >/dev/null 2>&1 && echo "沙盒已停止並刪除" || echo "沙盒本來就沒在跑"
    ;;
  status)
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      ready && echo "沙盒:執行中、可連線  127.0.0.1:${PORT}" \
            || echo "沙盒:容器在跑但還不能連線(可能還在啟動)"
      docker stats --no-stream --format '記憶體:{{.MemUsage}}' "$NAME" 2>/dev/null || true
    else
      echo "沙盒:未啟動   ($0 start)"
    fi
    ;;
  *) echo "用法:$0 {start|stop|status}" >&2; exit 2 ;;
esac
