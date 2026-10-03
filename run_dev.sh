#!/usr/bin/env bash
# 一键初始化 + 起服务（默认内联执行渲染任务；生产可拆 worker）
set -euo pipefail
cd "$(dirname "$0")"
python3 -m scripts.seed
exec python3 -m backend.server "${PORT:-8000}"
