"use client";

import { useTranslations } from "next-intl";

export default function TermsPage() {
  const t = useTranslations("legal");
  const items = (key: string) => {
    const raw = t.raw(key);
    return Array.isArray(raw) ? raw as string[] : [];
  };

  return (
    <div className="mx-auto max-w-3xl px-6 py-12">
      <h1 className="mb-8 text-3xl font-bold">{t("termsTitle")}</h1>
      <div className="prose max-w-none space-y-6 text-text-secondary prose-headings:text-text-primary prose-li:text-text-secondary">
        <section>
          <h2 className="text-xl font-semibold text-text-primary">{t("termsSection1Title")}</h2>
          <p>{t("termsSection1Desc")}</p>
        </section>
        <section>
          <h2 className="text-xl font-semibold text-text-primary">{t("termsSection2Title")}</h2>
          <p>{t("termsSection2Desc")}</p>
        </section>
        <section>
          <h2 className="text-xl font-semibold text-text-primary">{t("termsSection3Title")}</h2>
          <p>{t("termsSection3Desc")}</p>
        </section>
        <section>
          <h2 className="text-xl font-semibold text-text-primary">{t("termsSection4Title")}</h2>
          <ul className="list-disc space-y-1 pl-6">
            {items("termsSection4Items").map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
