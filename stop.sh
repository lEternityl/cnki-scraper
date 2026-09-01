#!/bin/bash
cd "$(dirname "$0")"

if [ -f .pid ]; then
  kill "$(cat .pid)" 2>/dev/null
  rm -f .pid
  echo "已停止（PID 文件）"
else
  # 兜底：按端口杀
  lsof -ti :8000 | xargs kill 2>/dev/null
  echo "已停止（端口 8000）"
fi
