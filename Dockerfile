FROM python:3.12-slim AS base

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY arb_scanner ./arb_scanner
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# discovery-only is the shipped default; never bake credentials into the image
ENV ARB_MODE=discovery-only

ENTRYPOINT ["arb-scanner"]
CMD ["scan", "--interval", "60"]
