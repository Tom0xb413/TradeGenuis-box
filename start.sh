#!/usr/bin/env bash
# TradeGenuis · 箱体突破看板 一键启动
# 用法：bash start.sh          （依赖未装会自动装，服务起在 http://127.0.0.1:8808）
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 未找到 python3，请先安装 Python 3.9+"; exit 1
fi

# 1) 依赖检查（akshare 用于东财失败时降级，装不上不阻断启动）
if ! $PY -c "import requests" >/dev/null 2>&1; then
  echo "⏳ 安装依赖 …"
  $PY -m pip install -q -r requirements.txt || { echo "❌ 安装失败，请手动执行: pip install -r requirements.txt"; exit 1; }
elif ! $PY -c "import akshare" >/dev/null 2>&1; then
  echo "⏳ 安装 akshare（东财不可用时的降级数据源）…"
  $PY -m pip install -q -r requirements.txt || echo "⚠️ akshare 安装失败，扫描仍可用新浪/腾讯/Gate 兜底"
fi

PORT="${PORT:-8808}"
HOST="${HOST:-127.0.0.1}"

# 2) 首次不再自动跑 A 股全市场（体量太大，且不进 180 根 K 线库）
#    全市场标的由 server 启动后约 25s 后台增量同步；A 股请在看板「标的/数据」里手动扫描。
if [ ! -f data/watchlist.json ]; then
  echo "ℹ️  无 data/watchlist.json：A 股页将为空，可在「标的/数据」中「立即扫描 A 股」。"
fi

# 3) 启动看板服务
echo "🚀 TradeGenuis 箱体突破看板启动中…"
echo "   地址: http://${HOST}:${PORT}"
echo "   停止: Ctrl+C"
exec $PY server.py --host "$HOST" --port "$PORT"
