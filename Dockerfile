# DocuRAG: REST API + MCP (streamable HTTP at /mcp) on port 8000.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    DOCURAG_HOME=/data \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra qdrant --no-install-project

COPY README.md ./
COPY docurag ./docurag
RUN uv sync --frozen --no-dev --extra qdrant

RUN useradd --create-home --uid 1000 docurag && mkdir -p /data && chown docurag /data
USER docurag
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

# Models download into /data/models on first use and persist in the volume.
CMD ["docurag", "serve", "--host", "0.0.0.0", "--port", "8000"]
