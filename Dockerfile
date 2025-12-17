# =============================================================================
# Celery Doorman - Dockerfile
# =============================================================================
# Build: docker build -t celery-doorman .
#
# Simulation mode (self-contained):
#   docker run --rm -it celery-doorman --simulate --workers 1
#   docker run --rm -it celery-doorman --simulate --workers 0 --enqueue 5
#
# Production mode:
#   docker run -d --name doorman \
#     --network your-project_default \
#     -e REDIS_URL=redis://redis:6379/0 \
#     -e SLACK_WEBHOOK_URL=https://hooks.slack.com/... \
#     celery-doorman --config config.yaml
# =============================================================================

FROM python:3.12-slim

LABEL maintainer="hchilabert@gmail.com"
LABEL description="Celery Doorman - Silent Queue Guardian"

# No buffer output
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install redis-server for simulation mode
RUN apt-get update && \
    apt-get install -y --no-install-recommends redis-server && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY celery_doorman.py .
COPY config.example.yaml ./config.yaml

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=5s --retries=3 \
    CMD python celery_doorman.py --once --dry-run || exit 1

ENTRYPOINT ["python", "celery_doorman.py"]
CMD ["--simulate", "--workers", "1"]
