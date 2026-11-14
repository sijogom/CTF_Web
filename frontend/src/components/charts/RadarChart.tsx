"use client";

import { useTranslations } from "next-intl";

interface RadarChartProps {
  data: { label: string; value: number; max: number }[];
  size?: number;
}

/* ──────────────────────────────────────────────
 * 분야별 레이더 차트 — SVG 기반 (육각형예시.png 스타일)
 *
 * - 외곽 그리드: 두꺼운 파란 실선 + 금색 꼭짓점 dot
 * - 내부 그리드: 연한 파란 점선 동심 다각형
 * - 데이터 영역: 반투명 파란 채우기 + 두꺼운 금색 테두리
 * - 0% 값도 최소 5%로 표시하여 영역이 항상 보이게 함
 * ────────────────────────────────────────────── */

/** 극좌표 → 데카르트 좌표 (12시 = 0도) */
function polarToXY(
  cx: number,
  cy: number,
  r: number,
  index: number,
  total: number,
): { x: number; y: number } {
  const angle = (2 * Math.PI * index) / total - Math.PI / 2;
  return {
    x: cx + Math.cos(angle) * r,
    y: cy + Math.sin(angle) * r,
  };
}

function toPointsStr(pts: { x: number; y: number }[]): string {
  return pts.map((p) => `${p.x},${p.y}`).join(" ");
}

export function RadarChart({ data, size = 260 }: RadarChartProps) {
  const tp = useTranslations("profile");
  const tc = useTranslations("common");
  const n = data.length;

  if (n === 0) {
    return (
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        role="img"
        aria-label={tp("radarChart")}
      >
        <text
          x={size / 2}
          y={size / 2}
          textAnchor="middle"
          dominantBaseline="middle"
          style={{ fill: "var(--color-text-muted)", fontSize: 12 }}
        >
          {tc("noData")}
        </text>
      </svg>
    );
  }

  const cx = size / 2;
  const cy = size / 2;
  const radius = size * 0.34;
  const labelOffset = 26;

  const gridLevels = [0.2, 0.4, 0.6, 0.8, 1.0];

  // 외곽 꼭짓점
  const outerPoints = Array.from({ length: n }, (_, i) =>
    polarToXY(cx, cy, radius, i, n),
  );

  // 데이터 꼭짓점 — 0%도 최소 5%로 표시하여 채우기 영역이 항상 보이게
  const MIN_RATIO = 0.05;
  const dataPoints = data.map((d, i) => {
    const raw = d.max > 0 ? Math.min(d.value / d.max, 1) : 0;
    const ratio = Math.max(raw, MIN_RATIO);
    return polarToXY(cx, cy, radius * ratio, i, n);
  });

  // 라벨 위치
  const labelPositions = data.map((_, i) =>
    polarToXY(cx, cy, radius + labelOffset, i, n),
  );

  function getTextAnchor(i: number): "start" | "middle" | "end" {
    const angle = (2 * Math.PI * i) / n - Math.PI / 2;
    const cos = Math.cos(angle);
    if (cos > 0.3) return "start";
    if (cos < -0.3) return "end";
    return "middle";
  }

  const dataPolygon = toPointsStr(dataPoints);

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={tp("radarChart")}
      className="overflow-visible"
    >
      {/* ── 내부 그리드: 점선 동심 다각형 (파란) ── */}
      {gridLevels.map((level) => {
        const pts = Array.from({ length: n }, (_, i) =>
          polarToXY(cx, cy, radius * level, i, n),
        );
        const isOuter = level === 1.0;
        return (
          <polygon
            key={level}
            points={toPointsStr(pts)}
            fill="none"
            stroke="var(--color-accent-primary)"
            strokeWidth={isOuter ? 2.5 : 0.8}
            strokeDasharray={isOuter ? "none" : "5 3"}
            opacity={isOuter ? 0.6 : 0.3}
          />
        );
      })}

      {/* ── 축 선 ── */}
      {outerPoints.map((end, i) => (
        <line
          key={i}
          x1={cx}
          y1={cy}
          x2={end.x}
          y2={end.y}
          stroke="var(--color-accent-primary)"
          strokeWidth={0.7}
          opacity={0.2}
        />
      ))}

      {/* ── 데이터 영역: 반투명 파란 채우기 + 두꺼운 금색 테두리 ── */}
      <polygon
        points={dataPolygon}
        fill="var(--color-accent-primary)"
        fillOpacity={0.3}
        stroke="var(--color-accent-warning)"
        strokeWidth={3}
        strokeLinejoin="round"
      />

      {/* ── 데이터 꼭짓점 (금색) ── */}
      {dataPoints.map((p, i) => (
        <g key={i}>
          <circle cx={p.x} cy={p.y} r={5} fill="var(--color-accent-warning)" opacity={0.3} />
          <circle cx={p.x} cy={p.y} r={3.5} fill="var(--color-accent-warning)" />
        </g>
      ))}

      {/* ── 외곽 꼭짓점 (금색 dot) ── */}
      {outerPoints.map((p, i) => (
        <circle
          key={i}
          cx={p.x}
          cy={p.y}
          r={5}
          fill="var(--color-accent-warning)"
          stroke="var(--color-bg-surface)"
          strokeWidth={2}
        />
      ))}

      {/* ── 카테고리 라벨 ── */}
      {labelPositions.map((pos, i) => (
        <text
          key={i}
          x={pos.x}
          y={pos.y}
          textAnchor={getTextAnchor(i)}
          dominantBaseline="middle"
          style={{
            fill: "var(--color-text-secondary)",
            fontSize: 11,
            fontWeight: 600,
          }}
        >
          {data[i].label}
        </text>
      ))}
    </svg>
  );
}
