"use client";

import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { useSiteSettings } from "@/providers/SiteSettingsProvider";

/* ─────────────────────────────────────────────
 * 푸터 섹션 데이터
 * ───────────────────────────────────────────── */
const FOOTER_SECTIONS = [
  {
    titleKey: "footer.about",
    links: [
      { labelKey: "footer.introduce", href: "/about" },
      { labelKey: "footer.contact", href: "/contact" },
      { labelKey: "footer.inquiry", href: "/inquiry" },
    ],
  },
  {
    titleKey: "footer.legal",
    links: [
      { labelKey: "footer.terms", href: "/terms" },
      { labelKey: "footer.privacy", href: "/privacy" },
    ],
  },
  {
    titleKey: "footer.resources",
    links: [
      { labelKey: "footer.github", href: "https://github.com", external: true },
    ],
  },
] as const;

/* ─────────────────────────────────────────────
 * 퀵 링크 — 주요 네비게이션 미러
 * ───────────────────────────────────────────── */
const QUICK_LINKS = [
  { labelKey: "nav.learn", href: "/learn" },
  { labelKey: "nav.wargame", href: "/wargame" },
  { labelKey: "nav.ctf", href: "/ctf" },
  { labelKey: "nav.community", href: "/community" },
  { labelKey: "nav.ranking", href: "/ranking" },
] as const;

export function Footer() {
  const t = useTranslations();
  const { siteName } = useSiteSettings();

  return (
    <footer className="relative mt-auto overflow-hidden">
      {/* 상단 그라디언트 세퍼레이터 — 헤더의 하단 세퍼레이터와 대칭 */}
      <div className="h-px bg-gradient-to-r from-transparent via-accent-primary/30 to-transparent" />

      {/* 그리드 배경 패턴 */}
      <div className="footer-grid-bg">
        <div className="bg-bg-surface/95 backdrop-blur-sm">

          {/* 메인 콘텐츠 */}
          <div className="mx-auto max-w-7xl px-6 pt-12 pb-8">
            <div className="grid grid-cols-1 gap-10 sm:grid-cols-2 lg:grid-cols-12">

              {/* 브랜드 영역 — 넓은 공간 */}
              <div className="lg:col-span-4">
                {/* 로고 */}
                <div className="flex items-center gap-2.5">
                  <svg className="h-7 w-7 text-accent-primary" viewBox="0 0 32 32" fill="none">
                    <path
                      d="M16 2L4 7v8c0 7.73 5.12 14.96 12 17 6.88-2.04 12-9.27 12-17V7L16 2z"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      fill="none"
                    />
                    <path
                      d="M16 8v5m0 6v5m-5-11h10m-8-3l3 3-3 3m6-6l-3 3 3 3"
                      stroke="currentColor"
                      strokeWidth="1"
                      strokeLinecap="round"
                      className="text-accent-success"
                      opacity="0.7"
                    />
                    <circle cx="16" cy="16" r="2.5" className="fill-accent-primary" />
                    <circle cx="16" cy="16" r="1" className="fill-bg-surface" />
                  </svg>
                  <span className="logo-gradient text-base font-bold tracking-tight">
                    {siteName}
                  </span>
                </div>

                {/* 설명 */}
                <p className="mt-4 max-w-xs text-sm leading-relaxed text-text-muted">
                  {t("meta.description")}
                </p>

                {/* 상태 인디케이터 */}
                <div className="mt-5 flex items-center gap-2 font-mono text-[11px] text-text-muted">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent-success animate-glow-pulse" />
                  <span>
                    <span className="text-accent-success">{t("footer.status")}</span>
                    <span className="mx-1 text-border-light">::</span>
                    <span>{t("footer.operational")}</span>
                  </span>
                </div>
              </div>

              {/* 퀵 네비게이션 */}
              <div className="lg:col-span-2">
                <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-text-secondary">
                  <span className="h-px w-3 bg-accent-primary" />
                  {t("footer.navigate")}
                </h3>
                <ul className="mt-4 space-y-2.5">
                  {QUICK_LINKS.map((link) => (
                    <li key={link.labelKey}>
                      <Link
                        href={link.href}
                        className="group flex items-center gap-1.5 text-sm text-text-muted transition-colors hover:text-accent-primary"
                      >
                        <span className="font-mono text-[10px] text-text-muted/50 transition-colors group-hover:text-accent-primary/50">
                          /
                        </span>
                        {t(link.labelKey)}
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>

              {/* 기존 푸터 섹션들 */}
              {FOOTER_SECTIONS.map((section) => (
                <div key={section.titleKey} className="lg:col-span-2">
                  <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-text-secondary">
                    <span className="h-px w-3 bg-accent-primary" />
                    {t(section.titleKey)}
                  </h3>
                  <ul className="mt-4 space-y-2.5">
                    {section.links.map((link) => (
                      <li key={link.labelKey}>
                        <Link
                          href={link.href}
                          className="group flex items-center gap-1.5 text-sm text-text-muted transition-colors hover:text-text-primary"
                          {...("external" in link
                            ? { target: "_blank", rel: "noopener noreferrer" }
                            : {})}
                        >
                          {t(link.labelKey)}
                          {"external" in link && (
                            <svg className="h-3 w-3 text-text-muted/50" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                              <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6M15 3h6v6M10 14L21 3" />
                            </svg>
                          )}
                        </Link>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </div>

          {/* 하단 바 */}
          <div className="border-t border-border/50">
            <div className="mx-auto flex max-w-7xl flex-col items-center justify-between gap-3 px-6 py-5 sm:flex-row">
              {/* 저작권 */}
              <p className="font-mono text-[11px] text-text-muted">
                <span className="text-accent-primary/60">&copy;</span>{" "}
                {new Date().getFullYear()} {siteName}
                <span className="mx-1.5 text-border-light">|</span>
                {t("footer.allRightsReserved")}
              </p>
            </div>
          </div>

        </div>
      </div>
    </footer>
  );
}
