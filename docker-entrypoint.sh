#!/bin/sh
set -e

# Chromium's singleton lock records the hostname that created it, so a profile
# written by the host (or a previous container) makes a new container refuse to
# start. These locks are stale whenever this container is the only user.
PROFILE_DIR="${BROWSER_PROFILE_DIR:-/app/.browser_profile}"
mkdir -p "$PROFILE_DIR"
rm -f "$PROFILE_DIR/SingletonLock" "$PROFILE_DIR/SingletonCookie" "$PROFILE_DIR/SingletonSocket"

# Cloudflare rejects headless Chromium for RVSQ, so run headed by default.
# Xvfb ships with the Playwright image; start it directly (xvfb-run relies on
# a SIGUSR1 handshake that does not complete when xvfb-run is PID 1).
if [ "${RVSQ_HEADLESS:-0}" = "1" ]; then
    exec python main.py "$@"
fi

export DISPLAY=:99
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
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
