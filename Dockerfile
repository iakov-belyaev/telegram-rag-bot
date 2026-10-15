# telegram-rag-bot — production image
FROM python:3.11-slim

# OCR runtime (optional but enables photo OCR)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-rus curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY seeds.yaml.example ./

# State, sessions, logs are volumes (see docker-compose.yml)
VOLUME ["/app/state", "/app/logs"]

# Bind API to all interfaces inside the container (compose maps to localhost only)
ENV API_HOST=0.0.0.0

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8088/health || exit 1

CMD ["python", "main.py"]
