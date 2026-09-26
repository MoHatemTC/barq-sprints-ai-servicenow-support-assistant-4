# Shared image for both the FastAPI service and the Celery worker.
# docker-compose.yml overrides `command` per-service.

FROM python:3.14-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Dependency layer cached separately from source changes.
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src
COPY benchmark ./benchmark
RUN uv sync --no-dev

# Run as non-root (Celery refuses to run cleanly as root/superuser).
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

# Default command; overridden by docker-compose.yml for the worker service.
CMD ["uv", "run", "uvicorn", "barq_ai_support.main:app", "--host", "0.0.0.0", "--port", "8000"]
