"use client";

import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";

export default function ContactPage() {
  const t = useTranslations("contact");

  return (
    <div className="mx-auto max-w-3xl px-6 py-12">
      <h1 className="mb-2 text-3xl font-bold">{t("title")}</h1>
      <p className="mb-10 text-text-secondary">{t("desc")}</p>

      <div className="space-y-6">
        {/* 이메일 */}
        <div className="rounded-xl border border-border bg-bg-surface p-6">
          <h2 className="mb-2 text-lg font-semibold">{t("emailTitle")}</h2>
          <p className="mb-3 text-sm text-text-secondary">{t("emailDesc")}</p>
          <a
            href={`mailto:${t("emailValue")}`}
            className="inline-flex items-center gap-2 text-accent-primary hover:underline"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="2" y="4" width="20" height="16" rx="2" />
              <path d="M22 7l-10 7L2 7" />
            </svg>
            {t("emailValue")}
          </a>
        </div>

        {/* 커뮤니티 */}
        <div className="rounded-xl border border-border bg-bg-surface p-6">
          <h2 className="mb-2 text-lg font-semibold">{t("communityTitle")}</h2>
          <p className="mb-3 text-sm text-text-secondary">{t("communityDesc")}</p>
          <Link
            href="/community"
            className="inline-flex items-center gap-2 text-accent-primary hover:underline"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" />
              <circle cx="9" cy="7" r="4" />
              <path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" />
            </svg>
            {t("communityLink")}
          </Link>
        </div>

        {/* 버그 신고 */}
        <div className="rounded-xl border border-border bg-bg-surface p-6">
          <h2 className="mb-2 text-lg font-semibold">{t("bugTitle")}</h2>
          <p className="text-sm text-text-secondary">{t("bugDesc")}</p>
        </div>

        {/* 응답 안내 */}
        <div className="rounded-xl border border-border/50 bg-bg-primary/50 p-6">
          <h2 className="mb-2 text-lg font-semibold">{t("responseTitle")}</h2>
          <p className="text-sm text-text-secondary">{t("responseDesc")}</p>
        </div>
      </div>
    </div>
  );
}
