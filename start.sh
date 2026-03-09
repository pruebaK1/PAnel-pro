#!/bin/bash

# Crear directorio runtime para pulseaudio
mkdir -p /tmp/pulse-runtime
chmod 700 /tmp/pulse-runtime
export XDG_RUNTIME_DIR=/tmp/pulse-runtime
export PULSE_RUNTIME_PATH=/tmp/pulse-runtime

# Arrancar PulseAudio una sola vez al inicio
export DISPLAY=:99
pulseaudio --start --exit-idle-time=-1 --daemonize=yes --log-level=error 2>/dev/null || true
sleep 1

# Iniciar el panel
exec python3 /app/panel.py
