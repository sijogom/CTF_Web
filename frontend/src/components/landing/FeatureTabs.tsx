"use client";

import { useState } from "react";
import { Link } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

const FEATURE_KEYS = [
  {
    tabKey: "learnTab" as const,
    titleKey: "learnTitle" as const,
    pointsPrefix: "learnPoints" as const,
    link: "/learn",
    visual: "\u{1F4D6}",
  },
  {
    tabKey: "wargameTab" as const,
    titleKey: "wargameTitle" as const,
    pointsPrefix: "wargamePoints" as const,
    link: "/wargame",
    visual: "\u{1F3F4}",
  },
  {
    tabKey: "ctfTab" as const,
    titleKey: "ctfTitle" as const,
    pointsPrefix: "ctfPoints" as const,
    link: "/ctf",
    visual: "\u{1F3C6}",
  },
];

export function FeatureTabs() {
  const [active, setActive] = useState(0);
  const t = useTranslations("landing");
  const feature = FEATURE_KEYS[active];

  return (
    <section className="border-y border-border bg-bg-surface px-6 py-20">
      <div className="mx-auto max-w-5xl">
        <h2 className="mb-8 text-center text-3xl font-bold">
          {t("platformTitle")}
        </h2>

        {/* 탭 */}
        <div role="tablist" aria-label={t("platformTitle")} className="mb-10 flex justify-center gap-2">
          {FEATURE_KEYS.map((f, i) => (
            <button
              key={f.tabKey}
              role="tab"
              aria-selected={i === active}
              aria-controls="feature-tabpanel"
              onClick={() => setActive(i)}
              className={cn(
                "rounded-lg px-6 py-3 text-sm font-medium transition-colors",
                i === active
                  ? "bg-accent-primary text-white"
                  : "text-text-secondary hover:bg-bg-elevated"
              )}
            >
              {t(f.tabKey)}
            </button>
          ))}
        </div>

        {/* 내용 */}
        <div id="feature-tabpanel" role="tabpanel" className="grid grid-cols-1 items-center gap-12 lg:grid-cols-2">
          {/* 비주얼 */}
          <div className="flex items-center justify-center rounded-xl border border-border bg-bg-primary p-16 text-8xl">
            {feature.visual}
          </div>

          {/* 텍스트 */}
          <div>
            <h3 className="mb-4 text-2xl font-bold">{t(feature.titleKey)}</h3>
            <ul className="mb-6 space-y-3">
              {[0, 1, 2].map((idx) => (
                <li key={idx} className="flex items-start gap-3 text-text-secondary">
                  <span className="mt-0.5 text-accent-primary">{"\u2713"}</span>
                  <span>{t(`${feature.pointsPrefix}.${idx}`)}</span>
                </li>
              ))}
            </ul>
            <Link
              href={feature.link}
              className="text-sm font-medium text-accent-primary hover:underline"
            >
              {t("viewMore")}
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}
