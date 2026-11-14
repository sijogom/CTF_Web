interface StatCardProps {
  icon: string;
  label: string;
  value: string | number;
  change?: string;
}

export function StatCard({ icon, label, value, change }: StatCardProps) {
  return (
    <div className="rounded-xl border border-border bg-bg-surface p-5">
      <div className="flex items-center gap-3">
        <span className="text-2xl" aria-hidden="true">{icon}</span>
        <div>
          <p className="text-sm text-text-secondary">{label}</p>
          <p className="text-2xl font-bold">{value}</p>
          {change && <p className="text-xs text-accent-success">{change}</p>}
        </div>
      </div>
    </div>
  );
}
