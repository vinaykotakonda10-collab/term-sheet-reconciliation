# Container image for the reconciliation CLI.
#   docker build -t term-sheet-recon .
#   docker run --rm --env-file .env -v "$PWD/outputs:/app/outputs" term-sheet-recon
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src
COPY data ./data
COPY main.py test_groq_connection.py ./

# Run as an unprivileged user; outputs/logs/cache are the only writable paths.
RUN useradd --create-home appuser && mkdir -p outputs logs .cache && chown -R appuser /app
USER appuser

ENTRYPOINT ["python", "main.py"]
