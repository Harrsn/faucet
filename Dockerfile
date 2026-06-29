FROM python:3.12-slim

# guessit needs nothing exotic; keep the image lean
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY faucet ./faucet
COPY pyproject.toml .

# config + events live on a mounted volume; library + downloads mounted at runtime
VOLUME ["/config"]
EXPOSE 8088

ENV EVENTS_FILE=/config/events.jsonl \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=3).status==200 else 1)" || exit 1

CMD ["uvicorn", "faucet.app:app", "--host", "0.0.0.0", "--port", "8088"]
