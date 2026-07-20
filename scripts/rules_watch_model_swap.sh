#!/usr/bin/env bash
# Swap the remote llama-server model for tuning, with a verified restore path.
#
#   ./swap_model.sh stop-all          # stop prod 3B + any tuning server
#   ./swap_model.sh start <gguf> <ctx> <port>
#   ./swap_model.sh restore-prod      # bring the 3B back exactly as it was
#   ./swap_model.sh status
#
# Processes are created via WMI Win32_Process so they survive the ssh session.
set -uo pipefail

H=benja@192.168.174.133
BIN='C:\llama.cpp\llama-server.exe'
MODELS='C:\llama.cpp\models'
PROD_MODEL="$MODELS\\Llama-3.2-3B-Instruct-Q4_K_M.gguf"
PROD_ARGS="-ngl 99 -c 32768 --host 192.168.174.133 --port 8080"

ps() { ssh -o BatchMode=yes "$H" "powershell -NoProfile -Command \"$1\"" 2>&1 | tr -d '\r'; }

spawn() {  # spawn "<full command line>"
  ps "\$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='$1'}; 'pid=' + \$r.ProcessId"
}

case "${1:-status}" in
  stop-all)
    ps "Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 3; 'stopped'"
    ps "(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader)"
    ;;
  start)
    gguf="${2:?need gguf filename}"; ctx="${3:-4096}"; port="${4:-8081}"
    spawn "\"$BIN\" -m \"$MODELS\\$gguf\" -ngl 99 -c $ctx --host 192.168.174.133 --port $port"
    echo "waiting for load..."
    for _ in $(seq 1 40); do
      if ssh -o BatchMode=yes "$H" "curl.exe -s --max-time 5 http://192.168.174.133:$port/v1/models" 2>/dev/null | grep -q gguf; then
        echo "UP on $port"; break
      fi
      sleep 6
    done
    ps "(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader)"
    ;;
  restore-prod)
    spawn "\"$BIN\" -m \"$PROD_MODEL\" $PROD_ARGS"
    echo "waiting for prod 3B..."
    for _ in $(seq 1 30); do
      if curl -s --max-time 5 http://192.168.174.133:8080/v1/models 2>/dev/null | grep -q gguf; then
        echo "PROD RESTORED on 8080"; break
      fi
      sleep 5
    done
    ;;
  status)
    ps "Get-CimInstance Win32_Process -Filter \\\"Name='llama-server.exe'\\\" | Select-Object ProcessId,CommandLine | Format-List"
    ps "(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader)"
    ;;
  *) echo "unknown: $1"; exit 1 ;;
esac
