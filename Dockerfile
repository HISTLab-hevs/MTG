FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/HISTLab-hevs/MTG" \
    org.opencontainers.image.licenses="Apache-2.0"

COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Only runtime resources: never copy the operator's environment, uploaded
# documents, database, certificates, or other local files into a public image.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini LICENSE LICENSE-docker LICENSE-docs ./

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
