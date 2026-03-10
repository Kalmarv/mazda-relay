FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen 2>/dev/null || uv sync

# Copy submodule pymazda source
COPY vendor/ha-mazda/custom_components/mazda_cs/pymazda vendor/ha-mazda/custom_components/mazda_cs/pymazda/
COPY server.py test.py ./

EXPOSE 8200

CMD ["uv", "run", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8200"]
