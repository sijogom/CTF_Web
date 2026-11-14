import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

// 백엔드 호스트 — 개발 환경(Windows+WSL)에서는 WSL IP, Docker에서는 서비스명
const backendHost = process.env.BACKEND_HOST || "localhost";

// 서비스별 호스트 (Docker bridge 네트워크에서 각 서비스명으로 직접 접근)
const authHost = process.env.AUTH_HOST || backendHost;
const learnHost = process.env.LEARN_HOST || backendHost;
const communityHost = process.env.COMMUNITY_HOST || backendHost;
const envOrchHost = process.env.ENV_ORCH_HOST || backendHost;
const minioHost = process.env.MINIO_HOST || backendHost;

// 서비스 포트 — server.sh가 동적 배정 후 환경변수로 전달 (기본값은 선호 포트)
const authPort = process.env.AUTH_PORT || "8020";
const learnPort = process.env.LEARN_PORT || "8100";
const communityPort = process.env.COMMUNITY_PORT || "8300";
const envOrchPort = process.env.ENV_ORCH_PORT || "8200";
const minioPort = process.env.MINIO_PORT || "9000";

const nextConfig: NextConfig = {
  basePath: "/ctf",
  output: "standalone",
  // 상위 작업 폴더의 무관한 lockfile을 Turbopack workspace root로 추론하지 않는다.
  turbopack: {
    root: process.cwd(),
  },
  allowedDevOrigins: process.env.ALLOWED_DEV_ORIGINS
    ? process.env.ALLOWED_DEV_ORIGINS.split(",")
    : [],
  // 보안 헤더 — Clickjacking, MIME sniffing, XSS 등 브라우저 공격 방지
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-XSS-Protection", value: "1; mode=block" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
  // X-Powered-By 헤더 제거 (Next.js 노출 방지)
  poweredByHeader: false,
  async rewrites() {
    return [
      // auth-service 프록시
      { source: "/api/auth/:path*", destination: `http://${authHost}:${authPort}/:path*` },
      // learn-service 프록시 (대회 API는 auth-service에 구현 — 먼저 매칭)
      // 참가자에게 배정된 문제의 보호된 상세/첨부만 learn-service가 소유한다.
      // 구체 경로를 Auth catch-all보다 먼저 두어 로컬 Next dev와 production Caddy의
      // 서비스 책임을 일치시킨다.
      {
        source: "/api/learn/competitions/:competitionId/challenges/:challengeId/files/:fileIndex",
        destination: `http://${learnHost}:${learnPort}/competitions/:competitionId/challenges/:challengeId/files/:fileIndex`,
      },
      {
        source: "/api/learn/competitions/:competitionId/challenges/:challengeId/attachments/:assetId",
        destination: `http://${learnHost}:${learnPort}/competitions/:competitionId/challenges/:challengeId/attachments/:assetId`,
      },
      {
        source: "/api/learn/competitions/:competitionId/challenges/:challengeId",
        destination: `http://${learnHost}:${learnPort}/competitions/:competitionId/challenges/:challengeId`,
      },
      { source: "/api/learn/competitions/:path*", destination: `http://${authHost}:${authPort}/competitions/:path*` },
      { source: "/api/learn/:path*", destination: `http://${learnHost}:${learnPort}/:path*` },
      // community-service 프록시
      { source: "/api/community/posts/:path*", destination: `http://${communityHost}:${communityPort}/posts/:path*` },
      { source: "/api/community/reports/:path*", destination: `http://${communityHost}:${communityPort}/reports/:path*` },
      { source: "/api/community/moderation/:path*", destination: `http://${communityHost}:${communityPort}/moderation/:path*` },
      { source: "/api/community/marketplace/:path*", destination: `http://${communityHost}:${communityPort}/marketplace/:path*` },
      { source: "/api/community/inquiries/:path*", destination: `http://${communityHost}:${communityPort}/inquiries/:path*` },
      { source: "/api/community/inquiries", destination: `http://${communityHost}:${communityPort}/inquiries` },
      // teams는 auth-service에 구현되어 있음
      { source: "/api/community/teams/:path*", destination: `http://${authHost}:${authPort}/teams/:path*` },
      { source: "/api/community/teams", destination: `http://${authHost}:${authPort}/teams` },
      // env-orchestrator 프록시 (챌린지 실습 환경)
      { source: "/api/env/:path*", destination: `http://${envOrchHost}:${envOrchPort}/:path*` },
      // CTF 대회 — auth-service에서 관리
      { source: "/api/ctf/:path*", destination: `http://${authHost}:${authPort}/:path*` },
      // MinIO 스토리지 프록시 (배너 이미지 등)
      { source: "/storage/:path*", destination: `http://${minioHost}:${minioPort}/:path*` },
    ];
  },
};

export default withNextIntl(nextConfig);
