# syntax=docker/dockerfile:1
# =============================================================================
# Ken Interlink OS — web (Next.js standalone server)
#
# Build context is the REPOSITORY ROOT (docker-compose.yml: context: .), so
# every COPY path below is relative to the root. The root .dockerignore keeps
# node_modules, .next, secrets and research repos out of the context.
#
#   docker build -f apps/web/Dockerfile -t ken-web .
# =============================================================================

ARG NODE_IMAGE=node:26-slim

# ---- deps: install exactly what the workspace lockfile pins -----------------
FROM ${NODE_IMAGE} AS deps
WORKDIR /repo
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json .npmrc ./
COPY apps/web/package.json apps/web/package.json
RUN npm ci --workspace apps/web --include-workspace-root

# ---- build: next build -> .next/standalone ----------------------------------
FROM deps AS build
WORKDIR /repo
ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production
COPY apps/web apps/web
RUN npm run build --workspace apps/web

# ---- runtime: minimal, non-root ---------------------------------------------
FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

# Standalone output mirrors the workspace layout (outputFileTracingRoot is the
# repo root), so the server lives at apps/web/server.js. Static assets are not
# part of standalone output and are copied alongside it.
COPY --from=build --chown=node:node /repo/apps/web/.next/standalone ./
COPY --from=build --chown=node:node /repo/apps/web/.next/static ./apps/web/.next/static

# The official node image ships an unprivileged `node` user (uid 1000).
USER node
EXPOSE 3000

# KEN_API_BASE_URL is supplied at run time (compose sets http://api:8000) and is
# read server-side only; it is never baked into the image or the browser bundle.
CMD ["node", "apps/web/server.js"]
