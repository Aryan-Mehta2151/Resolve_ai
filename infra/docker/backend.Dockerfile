FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/apps/api:/workspace/apps/worker

WORKDIR /workspace

COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/api/app apps/api/app
COPY apps/api/alembic.ini apps/api/alembic.ini
COPY apps/api/alembic apps/api/alembic
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e ./apps/api

COPY apps/worker apps/worker

CMD ["uvicorn", "app.main:app", "--app-dir", "/workspace/apps/api", "--host", "0.0.0.0", "--port", "8000"]
