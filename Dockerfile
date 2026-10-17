FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    SCAN_WATCH_CONFIG=/config/settings.json \
    SCAN_WATCH_OUT=/scans \
    SCAN_WATCH_WEB=0.0.0.0:8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY scan_watch ./scan_watch
# /config and /scans are mount points for host folders (see compose.yml).
RUN pip install . \
    && mkdir -p /config /scans

USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
ENTRYPOINT ["scan-watch"]
CMD ["--no-menu"]
