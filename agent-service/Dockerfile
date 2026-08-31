FROM python:3.12-slim

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --extra redis --extra observability --extra policy --extra agents
COPY alembic.ini ./
COPY alembic ./alembic
COPY policies ./policies
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["uvicorn", "autoseguro.api:app", "--host", "0.0.0.0", "--port", "8080"]
