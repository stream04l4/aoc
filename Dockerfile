FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 ubuntu && useradd -u 1000 -g 1000 -m -s /bin/bash ubuntu

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen && chmod +x /app/docker/entrypoint.sh \
    && chown -R 1000:1000 /app

USER 1000:1000
EXPOSE 9200
ENTRYPOINT ["/app/docker/entrypoint.sh"]
