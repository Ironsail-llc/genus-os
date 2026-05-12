# Helm dashboard (Next.js 16). Dev mode — source mounted at runtime
# from compose so HMR picks up host edits.
FROM node:22-alpine

ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:$PATH"

RUN corepack enable && corepack prepare pnpm@9.4.0 --activate

WORKDIR /app

# NOTE: not copying pnpm-workspace.yaml — the dashboard runs as a
# standalone package inside the image. Including the workspace file
# without a `packages:` field makes pnpm 9 fail with "packages field missing".
COPY app/package.json app/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY app/ /app/

EXPOSE 3000

CMD ["pnpm", "exec", "next", "dev", "-H", "0.0.0.0"]
