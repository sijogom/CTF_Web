"use client";

import { useMemo, useState, useCallback, useRef } from "react";
import { useTranslations } from "next-intl";

/* ------------------------------------------------------------------ */
/*  타입                                                               */
/* ------------------------------------------------------------------ */

interface ActivityHeatmapProps {
  /** key = "YYYY-MM-DD", value = 활동 횟수 */
  data: Record<string, number>;
  /** 표시할 주(week) 수 (기본 26 = 약 6개월) */
  weeks?: number;
}

interface CellData {
  date: string;      // "YYYY-MM-DD"
  count: number;
  dayOfWeek: number; // 0=일 ~ 6=토
  month: number;     // 0~11
}

/* ------------------------------------------------------------------ */
/*  상수 & 유틸                                                        */
/* ------------------------------------------------------------------ */

/** 활동 레벨 → Tailwind 배경색 클래스 (theme 변수 활용) */
const LEVEL_COLORS = [
  "bg-bg-elevated",          // 0  — 빈 칸
  "bg-accent-primary/25",    // 1-2
  "bg-accent-primary/50",    // 3-5
  "bg-accent-primary/75",    // 6-9
  "bg-accent-primary",       // 10+
] as const;

function getLevel(count: number): number {
  if (count === 0) return 0;
  if (count <= 2) return 1;
  if (count <= 5) return 2;
  if (count <= 9) return 3;
  return 4;
}

/** 로컬 타임존 기준 "YYYY-MM-DD" 포매팅 */
function toDateStr(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** "2026-02-23" → "2026.02.23" */
function formatDateDot(dateStr: string): string {
  return dateStr.replace(/-/g, ".");
}

/* ------------------------------------------------------------------ */
/*  셀(날짜) 데이터 생성                                               */
/* ------------------------------------------------------------------ */

function buildGrid(data: Record<string, number>, weeks: number) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const todayDow = today.getDay(); // 0=일
  // 시작: weeks 전 일요일  →  끝: 오늘
  const totalDays = (weeks - 1) * 7 + todayDow + 1;

  const cells: CellData[] = [];
  let totalCount = 0;

  for (let i = totalDays - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    const dateStr = toDateStr(d);
    const count = data[dateStr] ?? 0;
    totalCount += count;
    cells.push({
      date: dateStr,
      count,
      dayOfWeek: d.getDay(),
      month: d.getMonth(),
    });
  }

  // 주(week) 열로 그룹핑
  // 각 주는 7칸 배열 (index = dayOfWeek, null = 빈 칸)
  const weekColumns: (CellData | null)[][] = [];
  let currentSlots: (CellData | null)[] = Array(7).fill(null);

  for (const cell of cells) {
    currentSlots[cell.dayOfWeek] = cell;
    if (cell.dayOfWeek === 6) {
      weekColumns.push(currentSlots);
      currentSlots = Array(7).fill(null);
    }
  }
  if (currentSlots.some((s) => s !== null)) {
    weekColumns.push(currentSlots);
  }

  // 월 라벨 위치 계산
  const monthLabels: { text: number; colIdx: number }[] = [];
  let prevMonth = -1;
  for (let col = 0; col < weekColumns.length; col++) {
    const firstCell = weekColumns[col].find((s) => s !== null);
    if (!firstCell) continue;
    if (firstCell.month !== prevMonth) {
      monthLabels.push({ text: firstCell.month, colIdx: col });
      prevMonth = firstCell.month;
    }
  }

  return { weekColumns, totalCount, monthLabels };
}

/* ------------------------------------------------------------------ */
/*  요일 라벨 (월/수/금만 표시 — GitHub 스타일)                         */
/* ------------------------------------------------------------------ */

const VISIBLE_DAY_INDICES = [1, 3, 5]; // 월, 수, 금

/* ------------------------------------------------------------------ */
/*  메인 컴포넌트                                                       */
/* ------------------------------------------------------------------ */

/**
 * GitHub 기여 그래프(잔디) 스타일 활동 히트맵.
 *
 * - 상단: 월 라벨
 * - 좌측: 요일 라벨 (월/수/금)
 * - 셀 hover 시 날짜+횟수 툴팁
 * - 하단: 범례(Less ~ More) + 총 활동 횟수
 */
export function ActivityHeatmap({ data, weeks = 26 }: ActivityHeatmapProps) {
  const t = useTranslations("profile");
  const tc = useTranslations("common");

  const monthNames: string[] = t.raw("heatmapMonths");
  const dayNames: string[] = t.raw("heatmapDays");

  const containerRef = useRef<HTMLDivElement>(null);
  const [tooltip, setTooltip] = useState<{
    text: string;
    x: number;
    y: number;
  } | null>(null);

  // 셀 & 주 데이터 계산
  const { weekColumns, totalCount, monthLabels } = useMemo(
    () => buildGrid(data, weeks),
    [data, weeks],
  );

  // 툴팁 이벤트
  const handleMouseEnter = useCallback(
    (cell: CellData, e: React.MouseEvent<HTMLDivElement>) => {
      const rect = e.currentTarget.getBoundingClientRect();
      const parentRect = containerRef.current?.getBoundingClientRect();
      if (!parentRect) return;

      const formatted = formatDateDot(cell.date);
      const text =
        cell.count > 0
          ? t("heatmapTooltip", { date: formatted, count: String(cell.count) })
          : t("heatmapTooltipNone", { date: formatted });

      setTooltip({
        text,
        x: rect.left - parentRect.left + rect.width / 2,
        y: rect.top - parentRect.top - 6,
      });
    },
    [t],
  );

  const handleMouseLeave = useCallback(() => setTooltip(null), []);

  return (
    <div className="flex flex-col gap-2">
      {/* 총 활동 수 요약 */}
      <p className="text-sm text-text-secondary">
        {t("heatmapTotal", { count: String(totalCount) })}
      </p>

      {/* 히트맵 본체 (스크롤 가능) */}
      <div className="relative overflow-x-auto" ref={containerRef}>
        <div className="inline-flex flex-col" role="img" aria-label={t("heatmap")}>
          {/* 월 라벨 행 */}
          <div className="flex" style={{ paddingLeft: 36 }}>
            {weekColumns.map((_, colIdx) => {
              const label = monthLabels.find((ml) => ml.colIdx === colIdx);
              return (
                <div
                  key={colIdx}
                  className="text-[10px] leading-none text-text-muted"
                  style={{ width: 15, flexShrink: 0 }}
                >
                  {label ? monthNames[label.text] : ""}
                </div>
              );
            })}
          </div>

          {/* 메인 그리드: 요일 라벨 + 셀 */}
          <div className="mt-1 flex">
            {/* 요일 라벨 열 (셀 그리드와 동일한 gap으로 정렬) */}
            <div
              className="flex flex-col gap-[3px]"
              style={{ width: 36, flexShrink: 0 }}
            >
              {Array.from({ length: 7 }).map((_, dayIdx) => (
                <div
                  key={dayIdx}
                  className="flex items-center text-[10px] leading-none text-text-muted"
                  style={{ height: 12 }}
                >
                  {VISIBLE_DAY_INDICES.includes(dayIdx)
                    ? dayNames[dayIdx]
                    : ""}
                </div>
              ))}
            </div>

            {/* 셀 그리드 */}
            <div className="flex gap-[3px]">
              {weekColumns.map((week, colIdx) => (
                <div key={colIdx} className="flex flex-col gap-[3px]">
                  {week.map((cell, rowIdx) => {
                    if (!cell) {
                      return (
                        <div
                          key={rowIdx}
                          style={{ width: 12, height: 12 }}
                        />
                      );
                    }
                    const level = getLevel(cell.count);
                    return (
                      <div
                        key={rowIdx}
                        className={`rounded-sm ${LEVEL_COLORS[level]} transition-opacity hover:opacity-80`}
                        style={{ width: 12, height: 12 }}
                        onMouseEnter={(e) => handleMouseEnter(cell, e)}
                        onMouseLeave={handleMouseLeave}
                      />
                    );
                  })}
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* 툴팁 */}
        {tooltip && (
          <div
            className="pointer-events-none absolute z-50 -translate-x-1/2 -translate-y-full rounded-md bg-bg-elevated px-2.5 py-1.5 text-xs font-medium text-text-primary shadow-lg ring-1 ring-border whitespace-nowrap"
            style={{ left: tooltip.x, top: tooltip.y }}
          >
            {tooltip.text}
            <div className="absolute left-1/2 top-full -translate-x-1/2 border-4 border-transparent border-t-bg-elevated" />
          </div>
        )}
      </div>

      {/* 범례 */}
      <div className="flex items-center gap-1.5 text-xs text-text-muted">
        <span>{tc("less")}</span>
        {LEVEL_COLORS.map((color, i) => (
          <div key={i} className={`h-3 w-3 rounded-sm ${color}`} />
        ))}
        <span>{tc("more")}</span>
      </div>
    </div>
  );
}
