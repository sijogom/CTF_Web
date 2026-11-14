import createMiddleware from "next-intl/middleware";
import { routing } from "./i18n/routing";

export default createMiddleware(routing);

export const config = {
  // api, _next, 정적파일, env/proxy(챌린지 프록시), ws(WebSocket) 경로 제외
  matcher: ["/((?!api|_next|env/proxy|ws/|storage|.*\\..*).*)"],
};
