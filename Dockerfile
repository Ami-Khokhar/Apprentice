# Container image for the hosted, bring-your-own-key deployment.
# The local install does not need this file; see README.md for running Apprentice locally.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

# Hugging Face Spaces runs the container as uid 1000.
RUN useradd --create-home --uid 1000 user
USER user
WORKDIR /home/user/app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PYTHONUNBUFFERED=1

# Resolve dependencies before the source, so edits do not invalidate this layer.
COPY --chown=user:user pyproject.toml uv.lock README.md ./
RUN uv sync --locked --extra hosted --no-install-project

COPY --chown=user:user apprentice ./apprentice
RUN uv sync --locked --extra hosted

# Hosted mode: each visitor supplies their own API key and gets their own practice history.
ENV APPRENTICE_MULTI_USER=true \
    APPRENTICE_DB_PATH=/home/user/app/apprentice.db

# Set APPRENTICE_PRACTICE_MODEL in the Space settings. Startup fails without it.

EXPOSE 8000
# Call the binary in the synced environment directly, so no resolve runs at boot.
# Hosts such as Render assign the port through PORT; fall back to 8000 locally.
CMD ["sh", "-c", "exec /home/user/app/.venv/bin/uvicorn apprentice.sidecar.app:build_app \
     --factory --host 0.0.0.0 --port ${PORT:-8000}"]
