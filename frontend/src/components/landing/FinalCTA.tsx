"use client";

import { Link } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { useAuth } from "@/providers/AuthProvider";

export function FinalCTA() {
  const t = useTranslations("landing");
  const th = useTranslations("home");
  const { user } = useAuth();

  return (
    <section className="border-t border-border bg-bg-surface px-6 py-20">
      <div className="mx-auto max-w-2xl text-center">
        <h2 className="mb-4 text-3xl font-bold">
          {t("ctaTitle")}
        </h2>
        <p className="mb-8 text-text-secondary">
          {t("ctaDesc")}
        </p>
        <Link href={user ? "/wargame" : "/register"}>
          <Button variant="primary" size="lg">
            {user ? th("explore") : t("ctaButton")}
          </Button>
        </Link>
      </div>
    </section>
  );
}
