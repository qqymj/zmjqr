#!/bin/bash
# 一键演示脚本：自动跑完小伴的完整功能演示
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-$(pwd)/.venv/bin/python}"

# ── 检查依赖 ────────────────────────────────────────────
if [[ ! -x "$PY" ]]; then
  echo "⚠  未发现 .venv，正在创建虚拟环境…"
  python3 -m venv .venv
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q httpx pytest pytest-asyncio ruff onnxruntime tokenizers
  echo "✓ 依赖就绪"
fi

echo "========================================================"
echo "  🤖 小伴 · 完整功能演示"
echo "  Python: $("$PY" --version)"
echo "  按 Ctrl+C 可中途退出"
echo "========================================================"

# ── 准备 FIFO ───────────────────────────────────────────
rm -f /tmp/xiaoban.in
mkfifo /tmp/xiaoban.in

# 演示输入序列（每条之间留 sleep 让机器人完整跑完一轮）
{
  sleep 1
  echo "你好呀小伴，我是小明"
  sleep 4
  echo "今天天气怎么样？"
  sleep 5
  echo "/expr happy 0.9"
  sleep 3
  echo "/expr sad 0.7"
  sleep 3
  echo "/sched"
  sleep 3
  echo "/sched fire morning_brief"
  sleep 8
  echo "/sched fire news_digest"
  sleep 8
  echo "/sched sense humidity 35"
  sleep 5
  echo "/mem"
  sleep 4
  echo "好的，今天谢谢你陪我聊天，再见"
  sleep 4
  echo "/quit"
} > /tmp/xiaoban.in &
HOLDER_PID=$!

# ── 启动机器人并实时输出 ───────────────────────────────
set +e
timeout 60 "$PY" -u -m robot.main --sim --fake < /tmp/xiaoban.in 2>&1
EXIT_CODE=$?
set -e

kill $HOLDER_PID 2>/dev/null || true
wait 2>/dev/null
rm -f /tmp/xiaoban.in

echo
echo "========================================================"
case $EXIT_CODE in
  0|124)  echo "✅ 演示完成（exit=$EXIT_CODE）" ;;
  *)      echo "❌ 异常退出（exit=$EXIT_CODE）" ;;
esac
echo "========================================================"
