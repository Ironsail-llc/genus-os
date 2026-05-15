# syntax=docker/dockerfile:1.7
# The Helm (Next.js 16, React 19, Dockview) — production image.
# Builds the Next.js standalone bundle for a small final layer.
#
# Stages:
#   - deps:    pnpm install with cache mount (fastest layer to invalidate)
#   - builder: pnpm build → .next/standalone
#   - runner:  copy standalone + static into a slim node image

# ─── deps ────────────────────────────────────────────────────────────────────
FROM node:22-alpine AS deps

ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:$PATH"

RUN corepack enable && corepack prepare pnpm@9.4.0 --activate

WORKDIR /app

COPY app/package.json app/pnpm-lock.yaml ./

RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store \
    && pnpm install --frozen-lockfile

# ─── builder ─────────────────────────────────────────────────────────────────
FROM node:22-alpine AS builder

ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:$PATH" \
    NEXT_TELEMETRY_DISABLED=1

RUN corepack enable && corepack prepare pnpm@9.4.0 --activate

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY app/ ./

RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    --mount=type=cache,id=next-cache,target=/app/.next/cache \
    pnpm build

# ─── runner ──────────────────────────────────────────────────────────────────
FROM node:22-alpine AS runner

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

WORKDIR /app

RUN addgroup -g 1001 -S nextjs && adduser -S -G nextjs -u 1001 nextjs

# Next.js standalone bundle contains the minimal node_modules subset.
COPY --from=builder --chown=nextjs:nextjs /app/public ./public
COPY --from=builder --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

CMD ["node", "server.js"]
