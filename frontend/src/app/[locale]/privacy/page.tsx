"use client";

import { useTranslations } from "next-intl";

export default function PrivacyPage() {
  const t = useTranslations("legal");
  const items = (key: string) => {
    const raw = t.raw(key);
    return Array.isArray(raw) ? raw as string[] : [];
  };

  return (
    <div className="mx-auto max-w-3xl px-6 py-12">
      <h1 className="mb-8 text-3xl font-bold">{t("privacyTitle")}</h1>
      <div className="prose max-w-none space-y-6 text-text-secondary prose-headings:text-text-primary prose-li:text-text-secondary">
        <section>
          <h2 className="text-xl font-semibold text-text-primary">{t("privacySection1Title")}</h2>
          <p>{t("privacySection1Desc")}</p>
          <ul className="list-disc space-y-1 pl-6">
            {items("privacySection1Items").map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </section>
        <section>
          <h2 className="text-xl font-semibold text-text-primary">{t("privacySection2Title")}</h2>
          <ul className="list-disc space-y-1 pl-6">
            {items("privacySection2Items").map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </section>
        <section>
          <h2 className="text-xl font-semibold text-text-primary">{t("privacySection3Title")}</h2>
          <p>{t("privacySection3Desc")}</p>
        </section>
        <section>
          <h2 className="text-xl font-semibold text-text-primary">{t("privacySection4Title")}</h2>
          <ul className="list-disc space-y-1 pl-6">
            {items("privacySection4Items").map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
