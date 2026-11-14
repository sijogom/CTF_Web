"use client";

import { Link } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { useAuth } from "@/providers/AuthProvider";
import { ScenarioTerminal } from "./ScenarioTerminal";
import { heroScenario } from "./scenarios/heroScenario";

export function TerminalHero() {
  const t = useTranslations("home");
  const { user } = useAuth();

  return (
    <section className="flex min-h-[80vh] items-center justify-center px-6 py-20">
      <div className="grid max-w-7xl grid-cols-1 items-center gap-12 lg:grid-cols-[1.2fr_1fr]">
        {/* 터미널 */}
        <ScenarioTerminal scenario={heroScenario} className="w-full max-w-[620px]" />

        {/* 우측: 텍스트 + CTA */}
        <div className="space-y-6 text-center lg:text-left">
          <h1 className="text-4xl font-bold leading-tight text-text-primary lg:text-5xl">
            {t("hero")}
          </h1>
          <p className="text-lg leading-relaxed text-text-secondary">{t("subtitle")}</p>
          <div className="flex flex-wrap justify-center gap-4 lg:justify-start">
            <Link href={user ? "/wargame" : "/register"}>
              <Button variant="primary" size="lg">
                {user ? t("explore") : t("cta")}
              </Button>
            </Link>
            <Link href={user ? "/learn" : "/wargame"}>
              <Button variant="secondary" size="lg">
                {user ? t("learnCta") : t("explore")}
              </Button>
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}
