#!/bin/bash
mkdir -p /tmp/pulse-runtime
chmod 700 /tmp/pulse-runtime
export XDG_RUNTIME_DIR=/tmp/pulse-runtime
export PULSE_RUNTIME_PATH=/tmp/pulse-runtime
export DISPLAY=:99

pulseaudio --start --exit-idle-time=-1 --daemonize=yes --log-level=error 2>/dev/null || true
sleep 1

# Recolector de zombies cada 5 minutos
(while true; do wait; sleep 300; done) &

exec python3 /app/panel.py
