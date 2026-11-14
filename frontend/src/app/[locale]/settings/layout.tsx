"use client";

import { Link, usePathname } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { AccountContentBoundary } from "@/components/layout/AccountContentBoundary";

const TAB_KEYS = ["profile", "security", "notifications", "account"] as const;
const TAB_HREFS: Record<string, string> = {
  profile: "/settings/profile",
  security: "/settings/security",
  notifications: "/settings/notifications",
  account: "/settings/account",
};

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  const t = useTranslations("settings");
  const pathname = usePathname();

  // i18n usePathname은 이미 locale prefix 없이 반환
  const normalizedPath = pathname;

  return (
    <AccountContentBoundary>
    <div className="mx-auto max-w-4xl">
      <h1 className="mb-6 text-2xl font-bold">{t("title")}</h1>
      <div className="flex flex-col gap-6 sm:flex-row">
        {/* 사이드 탭 */}
        <nav role="tablist" aria-label={t("title")} className="flex shrink-0 gap-2 overflow-x-auto sm:w-48 sm:flex-col">
          {TAB_KEYS.map((key) => {
            const isActive = normalizedPath.endsWith(`/settings/${key}`) || normalizedPath.endsWith(`/settings/${key}/`);
            return (
              <Link
                key={key}
                href={TAB_HREFS[key]}
                role="tab"
                aria-selected={isActive}
                className={`rounded-lg px-4 py-2.5 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-accent-primary text-white"
                    : "text-text-secondary hover:bg-bg-surface hover:text-text-primary"
                }`}
              >
                {t(`${key}Tab`)}
              </Link>
            );
          })}
        </nav>

        {/* 콘텐츠 */}
        <div className="flex-1">{children}</div>
      </div>
    </div>
    </AccountContentBoundary>
  );
}
