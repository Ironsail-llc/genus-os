# syntax=docker/dockerfile:1.7
# The Helm (Next.js 16, React 19, Dockview).
#
# Targets:
#   - dev:        next dev with HMR. Source intended to be bind-mounted by
#                 docker-compose (../app:/app). node_modules + .next survive
#                 via anonymous volumes declared in compose.
#   - production: Next.js standalone bundle, slim node image, non-root.
#                 Used by the CI build-and-push workflow.
#
# Default target is `production`.

# ─── deps — pnpm install, no source ──────────────────────────────────────────
FROM node:22-alpine AS deps

ARG PNPM_VERSION=10.34.5

ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:$PATH" \
    NEXT_TELEMETRY_DISABLED=1

RUN corepack enable && corepack prepare "pnpm@${PNPM_VERSION}" --activate

WORKDIR /app

COPY app/package.json app/pnpm-lock.yaml ./

RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store \
    && pnpm install --frozen-lockfile

# ─── dev — next dev with HMR (compose bind-mounts source) ────────────────────
# Source is intentionally NOT copied into this stage. docker-compose mounts
# ../app over /app at runtime, and anonymous volumes for /app/node_modules
# and /app/.next preserve the container-installed deps and dev cache:
#
#   volumes:
#     - ../app:/app
#     - /app/node_modules
#     - /app/.next
FROM deps AS dev

ENV PORT=3000 \
    HOSTNAME=0.0.0.0

EXPOSE 3000

CMD ["pnpm", "exec", "next", "dev", "-H", "0.0.0.0"]

# ─── builder — production build of Next.js standalone ────────────────────────
FROM deps AS builder

COPY app/ ./

RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    --mount=type=cache,id=next-cache,target=/app/.next/cache \
    pnpm build

# ─── production — slim node image with standalone bundle, non-root ───────────
FROM node:22-alpine AS production

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

WORKDIR /app

RUN addgroup -g 1001 -S nextjs && adduser -S -G nextjs -u 1001 nextjs

COPY --from=builder --chown=nextjs:nextjs /app/public ./public
COPY --from=builder --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

CMD ["node", "server.js"]
