"use client";

import { useTranslations } from "next-intl";

const TIMELINE_STEPS = [
  { week: "Week 1", titleKey: "step1Title" as const, descKey: "step1Desc" as const, icon: "\u{1F331}" },
  { week: "Week 2", titleKey: "step2Title" as const, descKey: "step2Desc" as const, icon: "\u{1F3F4}" },
  { week: "Week 4", titleKey: "step3Title" as const, descKey: "step3Desc" as const, icon: "\u{1F4C8}" },
  { week: "Week 8", titleKey: "step4Title" as const, descKey: "step4Desc" as const, icon: "\u{1F3C6}" },
];

export function GrowthTimeline() {
  const t = useTranslations("landing");

  return (
    <section className="px-6 py-20">
      <div className="mx-auto max-w-4xl">
        <h2 className="mb-12 text-center text-3xl font-bold">
          {t("journeyTitle")}
        </h2>
        <div className="relative">
          {/* 연결선 */}
          <div className="absolute left-1/2 top-0 hidden h-full w-0.5 -translate-x-1/2 bg-border lg:block" />

          <div className="space-y-12 lg:space-y-0">
            {TIMELINE_STEPS.map((step, i) => (
              <div
                key={step.week}
                className={`flex items-center gap-8 lg:gap-16 ${
                  i % 2 === 0 ? "lg:flex-row" : "lg:flex-row-reverse"
                }`}
              >
                {/* 내용 */}
                <div
                  className={`flex-1 rounded-xl border border-border bg-bg-surface p-6 ${
                    i % 2 === 0 ? "lg:text-right" : "lg:text-left"
                  }`}
                >
                  <span className="text-sm font-semibold text-accent-primary">
                    {step.week}
                  </span>
                  <h3 className="mt-1 text-lg font-bold">{t(step.titleKey)}</h3>
                  <p className="mt-2 text-sm text-text-secondary">{t(step.descKey)}</p>
                </div>

                {/* 아이콘 */}
                <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border-2 border-accent-primary bg-bg-primary text-2xl">
                  {step.icon}
                </div>

                {/* 빈 공간 (대칭) */}
                <div className="hidden flex-1 lg:block" />
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
