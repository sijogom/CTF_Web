import { createNavigation } from "next-intl/navigation";
import { routing } from "./routing";

/**
 * next-intl 네비게이션 유틸리티
 * — Link, redirect, usePathname, useRouter를 locale-aware 버전으로 제공
 * — 내부 링크에 자동으로 /{locale}/ 접두사를 붙여줌
 */
export const { Link, redirect, usePathname, useRouter } = createNavigation(routing);
