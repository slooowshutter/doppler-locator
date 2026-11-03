#!/usr/bin/env bash
# Keeps the app and a Cloudflare quick tunnel alive on this box. Usage: nohup deploy/run.sh &
cd "$(dirname "$0")/.."
mkdir -p data
while true; do
  if ! curl -sf -m 5 http://127.0.0.1:8787/healthz >/dev/null; then
    pkill -f "uvicorn app.main:app" ; sleep 1
    nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787 >> data/server.log 2>&1 &
    sleep 5
  fi
  if ! pgrep -f "cloudflared tunnel --url http://127.0.0.1:8787" >/dev/null; then
    nohup "$HOME/.local/bin/cloudflared" tunnel --no-autoupdate --url http://127.0.0.1:8787 >> data/tunnel.log 2>&1 &
    sleep 10
    grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' data/tunnel.log | tail -1 > data/public_url.txt
  fi
  sleep 30
done
