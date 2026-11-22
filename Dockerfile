FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py main.py notifier.py rvsq_checker.py ./
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Runs headed Chromium under Xvfb (see docker-entrypoint.sh): Cloudflare
# blocks headless browsers. ESC to headless via RVSQ_HEADLESS=1.
ENV PYTHONUNBUFFERED=1 \
    BROWSER_CHANNEL=chromium \
    TRACE_DIR=/app/traces \
    BROWSER_PROFILE_DIR=/app/.browser_profile

VOLUME ["/app/traces", "/app/.browser_profile"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD []
