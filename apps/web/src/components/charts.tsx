"use client";

/**
 * Chart primitives.
 *
 * Hand-rolled SVG rather than a charting library, for one reason: the mark
 * specs here are precise (2px strokes, 4px rounded data-ends anchored to the
 * baseline, 2px surface gaps between adjacent fills, recessive grid) and
 * bending a library's defaults into them is more code than drawing them.
 *
 * Every colour comes from a CSS custom property, so both themes resolve from
 * one definition and nothing is hardcoded to one surface.
 */

import { useId, useState } from "react";

export const SERIES = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
  "var(--series-5)",
  "var(--series-6)",
  "var(--series-7)",
  "var(--series-8)",
] as const;

type Point = { x: number; y: number | null };
export type Series = { name: string; points: Point[]; color: string };

const PAD = { top: 12, right: 12, bottom: 24, left: 44 };

function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0, 1];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;
  const ticks: number[] = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v);
  return ticks;
}

export function formatCompact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  if (value < 1 && value > 0) return value.toFixed(2);
  return String(Math.round(value));
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * Multi-series line chart with a crosshair tooltip.
 *
 * An HTML chart is interactive by nature, so the hover layer ships by default
 * rather than being an enhancement — reading an exact value off a pixel
 * position is not something a reader should have to do.
 */
export function LineChart({
  series,
  labels,
  height = 200,
  unit = "",
  formatValue = formatCompact,
}: {
  series: Series[];
  labels: string[];
  height?: number;
  unit?: string;
  formatValue?: (v: number) => string;
}) {
  const clipId = useId();
  const [hover, setHover] = useState<number | null>(null);
  const width = 720;

  const values = series.flatMap((s) => s.points.map((p) => p.y ?? 0));
  const max = Math.max(...values, 0);
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1] || 1;

  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const count = labels.length;

  const xAt = (i: number) => PAD.left + (count <= 1 ? plotW / 2 : (i / (count - 1)) * plotW);
  const yAt = (v: number) => PAD.top + plotH - (v / top) * plotH;

  if (count === 0) {
    return (
      <div
        className="grid place-items-center text-sm"
        style={{ height, color: "var(--ink-muted)" }}
      >
        No data in this window
      </div>
    );
  }

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full"
        style={{ height }}
        role="img"
        aria-label={`Line chart: ${series.map((s) => s.name).join(", ")}`}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const x = ((e.clientX - rect.left) / rect.width) * width;
          const i = Math.round(((x - PAD.left) / plotW) * (count - 1));
          setHover(Math.max(0, Math.min(count - 1, i)));
        }}
      >
        <defs>
          <clipPath id={clipId}>
            <rect x={PAD.left} y={PAD.top} width={plotW} height={plotH} />
          </clipPath>
        </defs>

        {/* Recessive grid — present for reading values, never competing with data */}
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={PAD.left}
              x2={width - PAD.right}
              y1={yAt(t)}
              y2={yAt(t)}
              stroke="var(--grid)"
              strokeWidth={1}
            />
            <text
              x={PAD.left - 8}
              y={yAt(t) + 4}
              textAnchor="end"
              fontSize={11}
              fill="var(--ink-muted)"
              className="tabular"
            >
              {formatValue(t)}
            </text>
          </g>
        ))}

        {hover !== null && (
          <line
            x1={xAt(hover)}
            x2={xAt(hover)}
            y1={PAD.top}
            y2={PAD.top + plotH}
            stroke="var(--axis)"
            strokeWidth={1}
          />
        )}

        <g clipPath={`url(#${clipId})`}>
          {series.map((s) => {
            const d = s.points
              .map((p, i) => `${i === 0 ? "M" : "L"}${xAt(i)},${yAt(p.y ?? 0)}`)
              .join(" ");
            return (
              <path
                key={s.name}
                d={d}
                fill="none"
                stroke={s.color}
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            );
          })}
        </g>

        {/* Hovered points get a surface ring so they read against any series
            crossing underneath them. */}
        {hover !== null &&
          series.map((s) => {
            const p = s.points[hover];
            if (!p || p.y === null) return null;
            return (
              <circle
                key={s.name}
                cx={xAt(hover)}
                cy={yAt(p.y)}
                r={4}
                fill={s.color}
                stroke="var(--surface)"
                strokeWidth={2}
              />
            );
          })}

        <line
          x1={PAD.left}
          x2={width - PAD.right}
          y1={PAD.top + plotH}
          y2={PAD.top + plotH}
          stroke="var(--axis)"
          strokeWidth={1}
        />

        {[0, Math.floor(count / 2), count - 1]
          .filter((i, idx, arr) => arr.indexOf(i) === idx && labels[i])
          .map((i) => (
            <text
              key={i}
              x={xAt(i)}
              y={height - 6}
              textAnchor={i === 0 ? "start" : i === count - 1 ? "end" : "middle"}
              fontSize={11}
              fill="var(--ink-muted)"
              className="tabular"
            >
              {formatTime(labels[i])}
            </text>
          ))}
      </svg>

      {hover !== null && (
        <div
          className="pointer-events-none absolute top-2 rounded-md px-3 py-2 text-xs shadow-sm"
          style={{
            left: `${(xAt(hover) / width) * 100}%`,
            transform:
              hover > count / 2 ? "translateX(calc(-100% - 10px))" : "translateX(10px)",
            background: "var(--surface-raised)",
            border: "1px solid var(--border)",
            color: "var(--ink)",
          }}
        >
          <div style={{ color: "var(--ink-muted)" }} className="mb-1 tabular">
            {new Date(labels[hover]).toLocaleString()}
          </div>
          {series.map((s) => (
            <div key={s.name} className="flex items-center gap-2 whitespace-nowrap">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ background: s.color }}
                aria-hidden
              />
              <span style={{ color: "var(--ink-secondary)" }}>{s.name}</span>
              <span className="tabular ml-auto font-medium">
                {s.points[hover]?.y === null || s.points[hover]?.y === undefined
                  ? "—"
                  : `${formatValue(s.points[hover].y!)}${unit}`}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Single-series bar chart. Data-ends are rounded and anchored to the baseline. */
export function BarChart({
  values,
  labels,
  height = 160,
  color = "var(--series-1)",
  formatValue = formatCompact,
  unit = "",
}: {
  values: number[];
  labels: string[];
  height?: number;
  color?: string;
  formatValue?: (v: number) => string;
  unit?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const width = 720;
  const max = Math.max(...values, 0);
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1] || 1;

  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;

  if (values.length === 0) {
    return (
      <div
        className="grid place-items-center text-sm"
        style={{ height, color: "var(--ink-muted)" }}
      >
        No data in this window
      </div>
    );
  }

  // A 2px surface gap between adjacent bars, so neighbours never fuse.
  const slot = plotW / values.length;
  const barW = Math.max(1, slot - 2);

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full"
        style={{ height }}
        role="img"
        aria-label="Bar chart"
        onMouseLeave={() => setHover(null)}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={PAD.left}
              x2={width - PAD.right}
              y1={PAD.top + plotH - (t / top) * plotH}
              y2={PAD.top + plotH - (t / top) * plotH}
              stroke="var(--grid)"
              strokeWidth={1}
            />
            <text
              x={PAD.left - 8}
              y={PAD.top + plotH - (t / top) * plotH + 4}
              textAnchor="end"
              fontSize={11}
              fill="var(--ink-muted)"
              className="tabular"
            >
              {formatValue(t)}
            </text>
          </g>
        ))}

        {values.map((v, i) => {
          const h = top === 0 ? 0 : (v / top) * plotH;
          return (
            <rect
              key={i}
              x={PAD.left + i * slot + 1}
              y={PAD.top + plotH - h}
              width={barW}
              height={Math.max(h, v > 0 ? 1 : 0)}
              rx={Math.min(4, barW / 2)}
              fill={color}
              opacity={hover === null || hover === i ? 1 : 0.45}
              onMouseEnter={() => setHover(i)}
            />
          );
        })}

        <line
          x1={PAD.left}
          x2={width - PAD.right}
          y1={PAD.top + plotH}
          y2={PAD.top + plotH}
          stroke="var(--axis)"
          strokeWidth={1}
        />

        {[0, values.length - 1]
          .filter((i, idx, arr) => arr.indexOf(i) === idx && labels[i])
          .map((i) => (
            <text
              key={i}
              x={PAD.left + i * slot + barW / 2}
              y={height - 6}
              textAnchor={i === 0 ? "start" : "end"}
              fontSize={11}
              fill="var(--ink-muted)"
              className="tabular"
            >
              {formatTime(labels[i])}
            </text>
          ))}
      </svg>

      {hover !== null && (
        <div
          className="pointer-events-none absolute top-2 rounded-md px-3 py-2 text-xs shadow-sm"
          style={{
            left: `${((PAD.left + hover * slot) / width) * 100}%`,
            transform:
              hover > values.length / 2
                ? "translateX(calc(-100% - 10px))"
                : "translateX(10px)",
            background: "var(--surface-raised)",
            border: "1px solid var(--border)",
          }}
        >
          <div style={{ color: "var(--ink-muted)" }} className="tabular">
            {new Date(labels[hover]).toLocaleString()}
          </div>
          <div className="tabular font-medium">
            {formatValue(values[hover])}
            {unit}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Legend. Always present for two or more series, so identity is never carried
 * by colour alone.
 */
export function Legend({ series }: { series: { name: string; color: string }[] }) {
  if (series.length < 2) return null;
  return (
    <div className="flex flex-wrap items-center gap-4 text-xs">
      {series.map((s) => (
        <span key={s.name} className="flex items-center gap-1.5">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ background: s.color }}
            aria-hidden
          />
          <span style={{ color: "var(--ink-secondary)" }}>{s.name}</span>
        </span>
      ))}
    </div>
  );
}
