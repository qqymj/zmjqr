#!/bin/bash
# 启动小伴后台交互进程：FIFO 喂输入，日志写 /tmp/xiaoban.log
# 用法：scripts/run_xiaoban.sh start | stop | status | send "你好" | log | clean
set -euo pipefail

XIAOBAN_DIR=/workspace
FIFO=/tmp/xiaoban.in
LOG=/tmp/xiaoban.log
PID=/tmp/xiaoban.pid
HOLDER_PID=/tmp/xiaoban.holder.pid

cd "$XIAOBAN_DIR"

start() {
  if [[ -f $PID ]] && kill -0 "$(cat $PID)" 2>/dev/null; then
    echo "已在运行 (PID $(cat $PID))"
    return 0
  fi
  rm -f "$FIFO" "$LOG" "$PID" "$HOLDER_PID"
  mkfifo "$FIFO"
  # 保持 FIFO 写端打开，防止 EOF 让机器人退出
  (while true; do sleep 3600; done) > "$FIFO" &
  echo $! > "$HOLDER_PID"
  PYTHONUNBUFFERED=1 nohup "$XIAOBAN_DIR/.venv/bin/python" -m robot.main --sim --fake \
    < "$FIFO" > "$LOG" 2>&1 &
  echo $! > "$PID"
  sleep 2
  if kill -0 "$(cat $PID)" 2>/dev/null; then
    echo "✓ 启动成功 PID=$(cat $PID)  日志=$LOG"
  else
    echo "✗ 启动失败，最后日志："
    tail -20 "$LOG" 2>/dev/null || echo "(无日志)"
    stop
    return 1
  fi
}

stop() {
  [[ -f $PID ]] && kill "$(cat $PID)" 2>/dev/null && echo "kill $(cat $PID)" || true
  [[ -f $HOLDER_PID ]] && kill "$(cat $HOLDER_PID)" 2>/dev/null || true
  sleep 1
  pkill -f "robot.main.*--sim.*--fake" 2>/dev/null || true
  pkill -f "sleep 3600" 2>/dev/null || true
  rm -f "$FIFO" "$PID" "$HOLDER_PID"
  echo "已停止"
}

status() {
  if [[ -f $PID ]] && kill -0 "$(cat $PID)" 2>/dev/null; then
    echo "运行中 PID=$(cat $PID)"
  else
    echo "未运行"
  fi
}

send() {
  local msg="${1:-}"
  [[ -z $msg ]] && { echo "用法: send \"你好\""; return 1; }
  echo "$msg" > "$FIFO"
}

clean() {
  stop
  rm -f "$LOG"
  echo "日志已清"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  send) send "${2:-}" ;;
  log) tail -f "$LOG" ;;
  clean) clean ;;
  *) echo "用法: $0 start|stop|status|send|log|clean" ;;
esac
