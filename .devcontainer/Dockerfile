FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# Remove stale Yarn apt source shipped in the base image (expired GPG key)
RUN rm -f /etc/apt/sources.list.d/yarn.list

# System dependencies for scientific Python packages and SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    sqlite3 \
    libsqlite3-dev \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Pre-install Python dev tooling
RUN pip install --no-cache-dir \
    ruff \
    pytest \
    pytest-asyncio \
    ipython

WORKDIR /workspaces/SiftBot
