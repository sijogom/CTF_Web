"use client";

import { useTranslations } from "next-intl";
import { Link, usePathname } from "@/i18n/navigation";
import { cn } from "@/lib/utils";
import { useEducationAccess } from "@/components/teaching/EducationAccessProvider";

export function TeachingNavigation() {
  const t = useTranslations("education");
  const pathname = usePathname();
  const { permissions } = useEducationAccess();
  const items = [
    { href: "/teaching", label: t("nav.dashboard") },
    { href: "/teaching/classes", label: t("nav.classes") },
    ...(permissions?.is_platform_admin
      ? [{ href: "/teaching/roles", label: t("nav.roleGrants") }]
      : []),
  ];

  return (
    <nav aria-label={t("nav.label")} className="flex flex-wrap gap-2 border-b border-border pb-4">
      {items.map((item) => {
        const active = pathname === item.href || (item.href !== "/teaching" && pathname.startsWith(`${item.href}/`));
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "rounded-lg px-3 py-2 text-sm font-medium transition-colors",
              active ? "bg-accent-primary text-white" : "text-text-secondary hover:bg-bg-surface hover:text-text-primary",
            )}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
