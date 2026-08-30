#!/usr/bin/env bash
set -euo pipefail

# One-command local launcher for the Workbench control plane.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
BACKEND_HOST="${WORKBENCH_HOST:-127.0.0.1}"
BACKEND_PORT="${WORKBENCH_BACKEND_PORT:-8000}"
FRONTEND_PORT="${WORKBENCH_FRONTEND_PORT:-3000}"
RUNTIME_DIR="${WORKBENCH_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/free-claude-code-workbench}"
STATE_DIR="${WORKBENCH_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/free-claude-code/workbench}"
BACKEND_PID_FILE="$RUNTIME_DIR/backend.pid"
FRONTEND_PID_FILE="$RUNTIME_DIR/frontend.pid"
BACKEND_LOG="$RUNTIME_DIR/backend.log"
FRONTEND_LOG="$RUNTIME_DIR/frontend.log"
TOKEN_FILE="$RUNTIME_DIR/auth.token"
CODEX_HOME_DIR="$RUNTIME_DIR/codex-home"
STATE_FILE="${WORKBENCH_STATE:-$STATE_DIR/state.json}"
EVENT_LOG="${WORKBENCH_EVENT_LOG:-$STATE_DIR/events.jsonl}"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
PNPM_BIN="${PNPM_BIN:-$(command -v pnpm || true)}"

if [[ -z "$PNPM_BIN" && -x /home/gnen/.hermes/node/bin/pnpm ]]; then
  PNPM_BIN="/home/gnen/.hermes/node/bin/pnpm"
fi

die() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

require_commands() {
  [[ -n "$UV_BIN" && -x "$UV_BIN" ]] || die "找不到 uv"
  [[ -n "$PNPM_BIN" && -x "$PNPM_BIN" ]] || die "找不到 pnpm"
  command -v curl >/dev/null 2>&1 || die "找不到 curl"
  command -v setsid >/dev/null 2>&1 || die "找不到 setsid"
}

pid_from() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  local pid
  pid="$(<"$file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

pid_alive() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null
}

managed_alive() {
  local file="$1"
  local pid
  pid="$(pid_from "$file" 2>/dev/null || true)"
  [[ -n "$pid" ]] && pid_alive "$pid"
}

port_responds() {
  local url="$1"
  curl --silent --show-error --output /dev/null --max-time 1 "$url"
}

wait_for_url() {
  local url="$1"
  local attempts="${2:-30}"
  for ((i = 0; i < attempts; i++)); do
    if port_responds "$url"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

stop_group() {
  local file="$1"
  local label="$2"
  local pid
  pid="$(pid_from "$file" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    rm -f "$file"
    return 0
  fi
  if pid_alive "$pid"; then
    printf '正在停止 %s（PID %s）...\n' "$label" "$pid"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for ((i = 0; i < 20; i++)); do
      pid_alive "$pid" || break
      sleep 0.25
    done
  fi
  rm -f "$file"
}

read_token() {
  [[ -f "$TOKEN_FILE" ]] || return 1
  local token
  token="$(<"$TOKEN_FILE")"
  [[ -n "$token" ]] || return 1
  printf '%s\n' "$token"
}

status() {
  local backend_state="未运行"
  local frontend_state="未运行"
  if managed_alive "$BACKEND_PID_FILE"; then backend_state="运行中（PID $(<"$BACKEND_PID_FILE")）"; fi
  if managed_alive "$FRONTEND_PID_FILE"; then frontend_state="运行中（PID $(<"$FRONTEND_PID_FILE")）"; fi
  printf 'Workbench 后端：%s\n' "$backend_state"
  printf 'Workbench 前端：%s\n' "$frontend_state"
  printf '地址：http://%s:%s/\n' "$BACKEND_HOST" "$FRONTEND_PORT"
  if [[ -f "$TOKEN_FILE" ]]; then
    printf '登录 token：已生成（位于用户运行时目录，不在仓库中）\n'
  fi
  printf '日志目录：%s\n' "$RUNTIME_DIR"
}

start() {
  require_commands
  mkdir -p "$RUNTIME_DIR" "$STATE_DIR" "$CODEX_HOME_DIR"
  chmod 700 "$CODEX_HOME_DIR"

  if managed_alive "$BACKEND_PID_FILE" || managed_alive "$FRONTEND_PID_FILE"; then
    if managed_alive "$BACKEND_PID_FILE" && managed_alive "$FRONTEND_PID_FILE"; then
      status
      printf '打开：http://%s:%s/\n' "$BACKEND_HOST" "$FRONTEND_PORT"
      if token="$(read_token 2>/dev/null)"; then
        printf '登录 token（只显示在当前终端）：%s\n' "$token"
      fi
      return 0
    fi
    status
    die "检测到 launcher 只启动了部分服务；先执行 $0 stop 后再重试"
  fi

  if port_responds "http://$BACKEND_HOST:$BACKEND_PORT/api/auth/status"; then
    die "后端端口 $BACKEND_PORT 已被占用；为避免误杀其他进程，本次不接管"
  fi
  if port_responds "http://$BACKEND_HOST:$FRONTEND_PORT/"; then
    die "前端端口 $FRONTEND_PORT 已被占用；为避免误杀其他进程，本次不接管"
  fi

  local token="${WORKBENCH_AUTH_TOKEN:-}"
  if [[ -z "$token" ]]; then
    if ! token="$(read_token 2>/dev/null)"; then
      token="$($UV_BIN run python -c 'import secrets; print(secrets.token_urlsafe(24))')"
      umask 077
      printf '%s\n' "$token" > "$TOKEN_FILE"
    fi
  fi
  [[ -n "$token" ]] || die "无法生成临时 token"

  printf '正在启动 Workbench...\n'
  setsid env \
    WORKBENCH_AUTH_TOKEN="$token" \
    WORKBENCH_STATE="$STATE_FILE" \
    WORKBENCH_EVENT_LOG="$EVENT_LOG" \
    WORKBENCH_WORKSPACE_ROOT="$PROJECT_DIR" \
    WORKBENCH_CODEX_HOME="$CODEX_HOME_DIR" \
    WORKBENCH_ALLOWED_ORIGINS="http://$BACKEND_HOST:$FRONTEND_PORT" \
    "$UV_BIN" run uvicorn workbench.backend.main:app \
      --host "$BACKEND_HOST" --port "$BACKEND_PORT" --log-level warning \
      >"$BACKEND_LOG" 2>&1 < /dev/null &
  echo "$!" > "$BACKEND_PID_FILE"

  if ! wait_for_url "http://$BACKEND_HOST:$BACKEND_PORT/api/auth/status"; then
    stop_group "$BACKEND_PID_FILE" "后端"
    rm -f "$TOKEN_FILE"
    die "后端启动失败，请查看 $BACKEND_LOG"
  fi

  setsid env \
    VITE_BACKEND_PORT="$BACKEND_PORT" \
    "$PNPM_BIN" --dir "$PROJECT_DIR/workbench/frontend" dev \
      --host "$BACKEND_HOST" --port "$FRONTEND_PORT" \
      >"$FRONTEND_LOG" 2>&1 < /dev/null &
  echo "$!" > "$FRONTEND_PID_FILE"

  if ! wait_for_url "http://$BACKEND_HOST:$FRONTEND_PORT/"; then
    stop_group "$FRONTEND_PID_FILE" "前端"
    stop_group "$BACKEND_PID_FILE" "后端"
    rm -f "$TOKEN_FILE"
    die "前端启动失败，请查看 $FRONTEND_LOG"
  fi

  printf '\nWorkbench 已启动。\n'
  printf '打开：http://%s:%s/\n' "$BACKEND_HOST" "$FRONTEND_PORT"
  printf '登录 token（只显示在当前终端，不写入文件）：%s\n' "$token"
  printf '停止：%s stop\n' "$0"
  printf '状态：%s status\n' "$0"
}

logs() {
  mkdir -p "$RUNTIME_DIR"
  touch "$BACKEND_LOG" "$FRONTEND_LOG"
  tail -n 40 -f "$BACKEND_LOG" "$FRONTEND_LOG"
}

open_browser() {
  if [[ "${WORKBENCH_OPEN_BROWSER:-0}" == "1" ]] && command -v cmd.exe >/dev/null 2>&1; then
    cmd.exe /c start "" "http://$BACKEND_HOST:$FRONTEND_PORT/" >/dev/null 2>&1 || true
  fi
}

case "${1:-start}" in
  start)
    start
    open_browser
    ;;
  stop)
    stop_group "$FRONTEND_PID_FILE" "前端"
    stop_group "$BACKEND_PID_FILE" "后端"
    rm -f "$TOKEN_FILE"
    printf 'Workbench 已停止。\n'
    ;;
  status) status ;;
  logs) logs ;;
  *)
    printf '用法：%s {start|stop|status|logs}\n' "$0" >&2
    exit 2
    ;;
esac
