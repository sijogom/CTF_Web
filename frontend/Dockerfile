FROM node:24-alpine AS base

WORKDIR /app
COPY package*.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# 개발 모드
FROM base AS dev
EXPOSE 3100
ENV PORT=3100
CMD ["npm", "run", "dev", "--", "-p", "3100"]

# 프로덕션 빌드 — NEXT_PUBLIC_* 변수는 빌드 타임에 주입
FROM base AS builder
ARG NEXT_PUBLIC_SITE_URL=https://localhost
ARG NEXT_PUBLIC_ENV_PROXY_BASE=""
ARG NEXT_PUBLIC_ENV_ACCESS_MODE=proxy
ENV NEXT_PUBLIC_SITE_URL=$NEXT_PUBLIC_SITE_URL
ENV NEXT_PUBLIC_ENV_PROXY_BASE=$NEXT_PUBLIC_ENV_PROXY_BASE
ENV NEXT_PUBLIC_ENV_ACCESS_MODE=$NEXT_PUBLIC_ENV_ACCESS_MODE
RUN npm run build

FROM node:24-alpine AS production
RUN adduser --disabled-password --gecos "" --uid 1001 appuser
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
COPY --from=builder /app/messages ./messages
RUN find /app -type d -exec chmod 0755 {} +
USER appuser
EXPOSE 3100
ENV PORT=3100
CMD ["node", "server.js"]
