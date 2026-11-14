"use client";

import { useTranslations } from "next-intl";

export default function AboutPage() {
  const t = useTranslations("about");
  const items = (key: string) => {
    const raw = t.raw(key);
    return Array.isArray(raw) ? (raw as string[]) : [];
  };

  return (
    <div className="mx-auto max-w-3xl px-6 py-12">
      <h1 className="mb-2 text-3xl font-bold">{t("title")}</h1>
      <p className="mb-10 text-text-secondary">{t("heroDesc")}</p>

      <div className="space-y-8">
        <section>
          <h2 className="mb-3 text-xl font-semibold">{t("section1Title")}</h2>
          <p className="leading-relaxed text-text-secondary">{t("section1Desc")}</p>
        </section>

        <section>
          <h2 className="mb-3 text-xl font-semibold">{t("section2Title")}</h2>
          <ul className="list-disc space-y-2 pl-6 text-text-secondary">
            {items("features").map((feat, i) => (
              <li key={i}>{feat}</li>
            ))}
          </ul>
        </section>

        <section>
          <h2 className="mb-3 text-xl font-semibold">{t("section3Title")}</h2>
          <ul className="list-disc space-y-1 pl-6 text-text-secondary">
            {items("techStack").map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </section>

        <section>
          <h2 className="mb-3 text-xl font-semibold">{t("section4Title")}</h2>
          <p className="leading-relaxed text-text-secondary">{t("section4Desc")}</p>
        </section>
      </div>
    </div>
  );
}
