#!/usr/bin/env bash
# =============================================================================
# load-generator · 本地 debug 启动脚本
#
# 用途: 在本机 venv 里跑 locustfile.py, 验证 e2b sandbox + Playwright CDP 通路。
# 默认仅启用浏览器流量 (WebsiteBrowserUser)。
#
# 必填:
#   E2B_API_KEY              腾讯云 AGS API Key, 形如 ark_xxxxxxxx
#
# 可选:
#   E2B_DOMAIN               默认 your-e2b-domain.com
#   LOCUST_HOST              默认 https://example.com
#   LOCUST_USERS             默认 1
#   LOCUST_SPAWN_RATE        默认 1
#   LOCUST_RUN_TIME          默认 2m
#   LOCUST_LOGLEVEL          默认 INFO
#   FLAGD_HOST               默认 127.0.0.1
#   OTEL_SDK_DISABLED        默认 true (本地无 collector)
#   VENV_DIR                 默认 .venv
#   PYTHON                   默认 python3.12
#
# 用法:
#   E2B_API_KEY=ark_xxx ./run-local.sh
#   ./run-local.sh --setup-only    # 只初始化 venv
#
# 退出: Ctrl-C, locust 触发 user.on_stop -> sandbox.kill 清理远端沙箱
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# -- 解析参数 -----------------------------------------------------------------
SETUP_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --setup-only) SETUP_ONLY=true ;;
        -h|--help)
            sed -n '2,30p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

# -- 必填校验 -----------------------------------------------------------------
if [[ -z "${E2B_API_KEY:-}" ]]; then
    echo "ERROR: E2B_API_KEY is required (e.g. ark_xxxxxxxx)" >&2
    echo "       export E2B_API_KEY=ark_xxx, or prefix the command with it." >&2
    exit 1
fi

# -- 默认值 -------------------------------------------------------------------
PYTHON="${PYTHON:-python3.12}"
VENV_DIR="${VENV_DIR:-.venv}"

export E2B_DOMAIN="${E2B_DOMAIN:-your-e2b-domain.com}"
export LOCUST_BROWSER_TRAFFIC_ENABLED="${LOCUST_BROWSER_TRAFFIC_ENABLED:-true}"
export LOCUST_HOST="${LOCUST_HOST:-https://example.com}"
export FLAGD_HOST="${FLAGD_HOST:-127.0.0.1}"
export OTEL_SDK_DISABLED="${OTEL_SDK_DISABLED:-true}"

LOCUST_USERS="${LOCUST_USERS:-1}"
LOCUST_SPAWN_RATE="${LOCUST_SPAWN_RATE:-1}"
LOCUST_RUN_TIME="${LOCUST_RUN_TIME:-2m}"
LOCUST_LOGLEVEL="${LOCUST_LOGLEVEL:-INFO}"

# -- venv 初始化 (幂等) -------------------------------------------------------
# 优先用 uv (https://github.com/astral-sh/uv); USE_UV=0 强制 stdlib 路径
USE_UV="${USE_UV:-auto}"
if [[ "$USE_UV" == "auto" ]]; then
    if command -v uv >/dev/null 2>&1; then USE_UV=1; else USE_UV=0; fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[setup] creating venv at $VENV_DIR (USE_UV=$USE_UV) ..."
    if [[ "$USE_UV" == "1" ]]; then
        uv venv --python "$PYTHON" "$VENV_DIR"
    else
        "$PYTHON" -m venv "$VENV_DIR"
    fi
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# requirements.txt 变化就重装, 用文件 mtime 当 cache key
REQ_STAMP="$VENV_DIR/.requirements.stamp"
if [[ ! -f "$REQ_STAMP" || "requirements.txt" -nt "$REQ_STAMP" ]]; then
    echo "[setup] installing/updating dependencies ..."
    if [[ "$USE_UV" == "1" ]]; then
        uv pip install -q -r requirements.txt
    elif python -m pip --version >/dev/null 2>&1; then
        python -m pip install -q -U pip
        python -m pip install -q -r requirements.txt
    else
        echo "ERROR: neither 'uv' nor 'pip' is available in the venv ($VENV_DIR)." >&2
        echo "       Re-create the venv with: rm -rf $VENV_DIR && ./run-local.sh" >&2
        echo "       (or install uv: brew install uv / pipx install uv)" >&2
        exit 3
    fi
    touch "$REQ_STAMP"
fi

# 走 connect_over_cdp 连远端 e2b 沙箱里的 Chromium, 本地不需要
# `playwright install chromium`。

if [[ "$SETUP_ONLY" == "true" ]]; then
    echo "[setup] done. venv ready at $VENV_DIR. exit (--setup-only)."
    exit 0
fi

# -- 启动 locust --------------------------------------------------------------
echo "[run] LOCUST_HOST=$LOCUST_HOST"
echo "[run] E2B_DOMAIN=$E2B_DOMAIN  E2B_API_KEY=${E2B_API_KEY:0:6}***"
echo "[run] users=$LOCUST_USERS spawn_rate=$LOCUST_SPAWN_RATE run_time=$LOCUST_RUN_TIME"
echo "[run] press Ctrl-C to stop (will trigger sandbox.kill)"
echo

exec locust \
    -f locustfile.py \
    --host "$LOCUST_HOST" \
    --headless \
    --users "$LOCUST_USERS" \
    --spawn-rate "$LOCUST_SPAWN_RATE" \
    --run-time "$LOCUST_RUN_TIME" \
    --loglevel "$LOCUST_LOGLEVEL" \
    --skip-log-setup
