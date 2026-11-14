"use client";

import { useEffect, useState } from "react";

export function MaintenanceBanner() {
  const [isMaintenance, setIsMaintenance] = useState(false);

  useEffect(() => {
    async function check() {
      try {
        const res = await fetch("/api/auth/admin/maintenance");
        if (res.ok) {
          const data = await res.json();
          setIsMaintenance(data.maintenance);
        }
      } catch {
        // 서버 접속 불가 시 무시
      }
    }
    check();
    const interval = setInterval(check, 60_000); // 1분마다 체크
    return () => clearInterval(interval);
  }, []);

  if (!isMaintenance) return null;

  return (
    <div className="bg-accent-warning/10 border-b border-accent-warning px-4 py-2 text-center text-sm text-accent-warning">
      현재 시스템 점검이 진행 중입니다. 일부 기능이 제한될 수 있습니다.
    </div>
  );
}
