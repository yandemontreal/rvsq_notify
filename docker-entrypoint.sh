#!/bin/sh
set -e

# Cloudflare rejects headless Chromium for RVSQ, so run headed by default.
# Xvfb ships with the Playwright image; start it directly (xvfb-run relies on
# a SIGUSR1 handshake that does not complete when xvfb-run is PID 1).
if [ "${RVSQ_HEADLESS:-0}" = "1" ]; then
    exec python main.py "$@"
fi

export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1366x768x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &

i=0
while [ "$i" -lt 50 ]; do
    if [ -S "/tmp/.X11-unix/X99" ]; then
        break
    fi
    i=$((i + 1))
    sleep 0.2
done

exec python main.py --headed "$@"
