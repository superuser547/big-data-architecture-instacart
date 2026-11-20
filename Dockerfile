FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --python 3.11

EXPOSE 8501
CMD ["uv", "run", "streamlit", "run", "src/instacart/dashboard/app.py", "--server.address=0.0.0.0"]
