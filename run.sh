#!/usr/bin/env bash
# Start everything: config, static web server on :8080, and the agent worker (auto-restarts).
set -uo pipefail
cd "$(dirname "$0")"
./gen_config.sh
pgrep -f "http.server 8080" >/dev/null || (cd web && nohup python3 -m http.server 8080 >/dev/null 2>&1 &)
echo "Phone page:      http://localhost:8080/   (public: https://aryan-techie.github.io/twingate/)"
echo "Mission control: http://localhost:8080/control.html"
cd agent
while true; do
  .venv/bin/python worker.py
  echo "worker exited ($?); restarting in 2s"; sleep 2
done
