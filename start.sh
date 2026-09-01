#!/bin/bash
cd "$(dirname "$0")"

# 先停掉已有实例
if [ -f .pid ]; then
  kill "$(cat .pid)" 2>/dev/null
  rm -f .pid
  sleep 1
fi
# 兜底：按端口清理残留
lsof -ti :8000 | xargs kill 2>/dev/null
sleep 1

# 后台启动 uvicorn（nohup + disown 脱离终端，兼容 macOS）
nohup python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 \
  > server.log 2>&1 < /dev/null &
PID=$!
echo "$PID" > .pid
disown 2>/dev/null
sleep 2

if kill -0 "$PID" 2>/dev/null; then
  echo "CNKI 抓取系统已启动: http://127.0.0.1:8000"
  echo "日志: tail -f $(pwd)/server.log"
  echo "停止: ./stop.sh"
else
  echo "启动失败，请查看 server.log"
  exit 1
fi
