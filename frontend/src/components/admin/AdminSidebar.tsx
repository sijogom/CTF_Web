"use client";

import { Link } from "@/i18n/navigation";
import { usePathname } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  labelKey: string;
  icon: string;
  adminOnly?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { href: "/admin",              labelKey: "dashboard",             icon: "📊" },
  { href: "/admin/courses",      labelKey: "courseManagement",       icon: "📚" },
  { href: "/admin/challenges",   labelKey: "challengeManagement",   icon: "🏴" },
  { href: "/admin/competitions", labelKey: "competitionManagement",  icon: "🏆" },
  { href: "/admin/events",       labelKey: "eventManagement",       icon: "📅" },
  { href: "/admin/community",    labelKey: "communityManagement",   icon: "💬" },
  { href: "/admin/inquiries",    labelKey: "inquiryManagement",     icon: "💌" },
  { href: "/admin/containers",   labelKey: "containerManagement",   icon: "🐳", adminOnly: true },
  { href: "/admin/notifications", labelKey: "notificationManagement", icon: "🔔" },
  { href: "/admin/submissions",  labelKey: "submissionLogs",        icon: "📝" },
  { href: "/admin/audit",        labelKey: "auditLog",              icon: "📋" },
  { href: "/admin/data",         labelKey: "dataManagement",        icon: "💾", adminOnly: true },
  { href: "/admin/users",        labelKey: "userManagement",        icon: "👥", adminOnly: true },
  { href: "/admin/tags",         labelKey: "tagManagement",         icon: "🏷️" },
  { href: "/admin/knowledge-relations", labelKey: "knowledgeRelationManagement", icon: "🕸️" },
  { href: "/admin/email",        labelKey: "emailManagement",       icon: "📧", adminOnly: true },
  { href: "/admin/tiers",        labelKey: "tierManagement",        icon: "🎖️", adminOnly: true },
  { href: "/admin/settings",     labelKey: "siteSettings",          icon: "⚙️", adminOnly: true },
];

interface AdminSidebarProps {
  isSuperAdmin: boolean;
}

export function AdminSidebar({ isSuperAdmin }: AdminSidebarProps) {
  const pathname = usePathname();
  const t = useTranslations("admin");

  const visibleItems = NAV_ITEMS.filter(
    (item) => !item.adminOnly || isSuperAdmin
  );

  return (
    <aside className="w-full shrink-0 border-b border-border bg-bg-surface lg:w-60 lg:border-b-0 lg:border-r">
      <div className="p-4">
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-text-muted">
          {t("dashboard")}
        </h2>
        <nav className="flex flex-wrap gap-1 lg:flex-col lg:flex-nowrap">
          {visibleItems.map((item) => {
            const isActive =
              pathname === item.href ||
              (item.href !== "/admin" && pathname.startsWith(item.href));
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "min-w-0 flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-accent-primary/10 text-accent-primary"
                    : "text-text-secondary hover:bg-bg-elevated hover:text-text-primary"
                )}
              >
                <span aria-hidden="true">{item.icon}</span>
                {t(item.labelKey)}
              </Link>
            );
          })}
        </nav>
      </div>
    </aside>
  );
}
