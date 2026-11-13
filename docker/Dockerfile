# Build from the project root:  docker build -f docker/Dockerfile -t ts-bench:latest .
FROM node:20-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 git cloc ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Package managers repos may use. The node base image already ships Yarn 1
# (/usr/local/bin/yarn), so installing it again fails with EEXIST: only add pnpm.
# Global installs (not corepack) keep both available in the offline measure phase.
RUN npm install -g pnpm@9 \
 && yarn --version && pnpm --version

# Pinned measurement toolchain, kept apart from every repo's own node_modules.
WORKDIR /opt/bench
COPY tools/package.json tools/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY tools/eslint.config.mjs ./
COPY bench ./bench

ENV PYTHONPATH=/opt/bench \
    PYTHONUNBUFFERED=1 \
    CI=true \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    ADBLOCK=1 \
    DISABLE_OPENCOLLECTIVE=1

WORKDIR /work/repo
