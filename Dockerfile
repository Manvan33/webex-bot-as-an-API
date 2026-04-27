FROM python:3.13-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifests first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies into system Python (no venv inside container)
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source
COPY webex_bot_api.py WebexWSClient.py ./

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "webex_bot_api:app", "--host", "0.0.0.0", "--port", "8000"]
