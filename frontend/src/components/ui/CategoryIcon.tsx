import { cn } from "@/lib/utils";

const CATEGORY_CONFIG: Record<string, { icon: string; label: string; colorClass: string }> = {
  web:      { icon: "\u25CF", label: "Web",      colorClass: "text-category-web" },
  pwn:      { icon: "\u25B2", label: "Pwn",      colorClass: "text-category-pwn" },
  rev:      { icon: "\u25C6", label: "Rev",      colorClass: "text-category-rev" },
  crypto:   { icon: "\u2605", label: "Crypto",   colorClass: "text-category-crypto" },
  forensic: { icon: "\u25A0", label: "Forensic", colorClass: "text-category-forensic" },
  misc:     { icon: "\u2B1F", label: "Misc",     colorClass: "text-category-misc" },
};

/** 아이콘 크기별 Tailwind 클래스 */
const SIZE_CLASSES = {
  sm: "text-sm",   // w-4 h-4 상당
  md: "text-base",  // w-5 h-5 상당
  lg: "text-lg",   // w-6 h-6 상당
} as const;

interface CategoryIconProps {
  category: string;
  showLabel?: boolean;
  /** 아이콘 크기 (sm: 작게, md: 기본, lg: 크게) */
  size?: "sm" | "md" | "lg";
  className?: string;
}

export function CategoryIcon({ category, showLabel = false, size = "md", className }: CategoryIconProps) {
  const config = CATEGORY_CONFIG[category.toLowerCase()] || CATEGORY_CONFIG.misc;
  return (
    <span className={cn("inline-flex items-center gap-1", SIZE_CLASSES[size], config.colorClass, className)}>
      <span aria-hidden="true">{config.icon}</span>
      {showLabel && <span>{config.label}</span>}
    </span>
  );
}
