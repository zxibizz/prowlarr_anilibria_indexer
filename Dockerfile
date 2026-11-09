FROM python:3.12-alpine

# Build context is the repository root; only the proxy module goes into the image.
# Together with .dockerignore that keeps the context from dragging in config/.
WORKDIR /app
COPY proxy/app.py /app/app.py

ENV PROXY_PORT=8788 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Stateless and needs nothing writable, so it fits a read-only root filesystem and
# Kubernetes' "restricted" Pod Security profile. The uid is numeric on purpose:
# runAsNonRoot can only verify a numeric USER.
USER 1000:1000

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8788/healthz',timeout=4)"

CMD ["python3", "/app/app.py"]
