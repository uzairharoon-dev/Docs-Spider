# VHS + DocuRAG, for recording demo.gif. See demo/README.md.
FROM ghcr.io/charmbracelet/vhs:v0.10.0

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/docurag \
    PATH="/opt/docurag/bin:$PATH" \
    DOCURAG_HOME=/data \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY docurag ./docurag
RUN uv sync --frozen --no-dev --python 3.12 && rm -rf /root/.cache/uv

# A built index (the eval corpus), passed as a named build context, so the recording
# shows search results without a 10-minute crawl.
COPY --from=data . /data
# Model files copied from another OS can fail fastembed's size check (a warning on every
# run), so fetch them fresh here. This also warms the cache for the recording.
RUN rm -rf /data/models && docurag search warmup -k 1 > /dev/null

WORKDIR /vhs
