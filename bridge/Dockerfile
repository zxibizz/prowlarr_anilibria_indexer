FROM python:3.12-alpine

# The bridge only uses the standard library, so there is nothing to install.
WORKDIR /app
COPY app.py /app/app.py

ENV BRIDGE_PORT=8787 \
    PYTHONUNBUFFERED=1

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=4)"

CMD ["python3", "/app/app.py"]
