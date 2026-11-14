import { NextIntlClientProvider } from "next-intl";
import { getMessages } from "next-intl/server";
import { notFound } from "next/navigation";
import { routing } from "@/i18n/routing";
import { AccessibilityProvider } from "@/lib/accessibility/context";
import { MainLayout } from "@/components/layout/MainLayout";
import { ToastProvider } from "@/providers/ToastProvider";
import { AuthProvider } from "@/providers/AuthProvider";
import { CommandPalette } from "@/components/ui/CommandPalette";
import { AxeAccessibility } from "@/components/dev/AxeAccessibility";
import { SiteSettingsProvider } from "@/providers/SiteSettingsProvider";

export default async function LocaleLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  if (!routing.locales.includes(locale as (typeof routing.locales)[number])) notFound();

  const messages = await getMessages();

  const skipLabels: Record<(typeof routing.locales)[number], string> = {
    ko: "본문으로 건너뛰기",
    en: "Skip to content",
    ja: "本文へスキップ",
    zh: "跳至正文",
  };

  const safeLang = locale.replace(/[^a-z-]/gi, "");

  return (
    <>
      {/* locale별 lang 속성 설정 — 루트 레이아웃은 locale 접근 불가하므로 여기서 주입 */}
      <script
        dangerouslySetInnerHTML={{
          __html: `document.documentElement.lang="${safeLang}"`,
        }}
      />
      <a href="#main-content" className="skip-link">
        {skipLabels[locale as (typeof routing.locales)[number]] ?? "Skip to content"}
      </a>
      <NextIntlClientProvider messages={messages}>
        <AccessibilityProvider>
          <ToastProvider>
            <SiteSettingsProvider>
              <AuthProvider>
                <MainLayout>{children}</MainLayout>
                <CommandPalette />
              </AuthProvider>
            </SiteSettingsProvider>
          </ToastProvider>
        </AccessibilityProvider>
      </NextIntlClientProvider>
      {process.env.NODE_ENV === "development" && <AxeAccessibility />}

      {/* 색각 모드 SVG 필터 (시각적으로 숨김) */}
      <svg className="hidden" aria-hidden="true">
        <defs>
          <filter id="protanopia-filter">
            <feColorMatrix type="matrix" values="0.567,0.433,0,0,0 0.558,0.442,0,0,0 0,0.242,0.758,0,0 0,0,0,1,0" />
          </filter>
          <filter id="deuteranopia-filter">
            <feColorMatrix type="matrix" values="0.625,0.375,0,0,0 0.7,0.3,0,0,0 0,0.3,0.7,0,0 0,0,0,1,0" />
          </filter>
          <filter id="tritanopia-filter">
            <feColorMatrix type="matrix" values="0.95,0.05,0,0,0 0,0.433,0.567,0,0 0,0.475,0.525,0,0 0,0,0,1,0" />
          </filter>
        </defs>
      </svg>
    </>
  );
}
