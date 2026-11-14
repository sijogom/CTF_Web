import { cn } from "@/lib/utils";

const CATEGORY_COLORS: Record<string, string> = {
  web: "bg-category-web/20 text-category-web",
  pwn: "bg-category-pwn/20 text-category-pwn",
  rev: "bg-category-rev/20 text-category-rev",
  crypto: "bg-category-crypto/20 text-category-crypto",
  forensic: "bg-category-forensic/20 text-category-forensic",
  misc: "bg-category-misc/20 text-category-misc",
};

interface BadgeProps {
  category: string;
  className?: string;
}

export function CategoryBadge({ category, className }: BadgeProps) {
  const color =
    CATEGORY_COLORS[category.toLowerCase()] || CATEGORY_COLORS.misc;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md px-2 py-1 text-xs font-medium",
        color,
        className
      )}
    >
      {category}
    </span>
  );
}
