"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { BreakdownRow, MetricsResponse } from "@/lib/api";
import { BarChart, Legend, LineChart, SERIES, formatCompact } from "@/components/charts";
import {
  Card,
  CardTitle,
  Stat,
  TableShell,
  Td,
  Th,
  formatMs,
  formatPercent,
  formatUsd,
} from "@/components/ui";

const WINDOWS = ["15m", "1h", "6h", "24h", "7d", "30d"] as const;
const POLL_MS = 10_000;

/**
 * The live dashboard.
 *
 * Polls rather than holding a WebSocket. Ten-second freshness is what a metrics
 * view actually needs, and polling has no connection state to manage, no
 * reconnect path, and no sticky-session problem when the API scales to N pods.
 * A live *trace tail* would be a genuine case for SSE; a metrics grid is not.
 */
export function MetricsView({
  project,
  initialMetrics,
  initialBreakdown,
  window: selectedWindow,
}: {
  project: string;
  initialMetrics: MetricsResponse;
  initialBreakdown: BreakdownRow[];
  window: string;
}) {
  const router = useRouter();
  const [metrics, setMetrics] = useState(initialMetrics);
  const [breakdown, setBreakdown] = useState(initialBreakdown);
  const [live, setLive] = useState(true);

  // No effect syncs props into state here. When the window changes the parent
  // remounts this component with a `key`, so fresh props become fresh initial
  // state — which is React's own answer to "reset state when a prop changes"
  // and avoids the cascading render an effect-plus-setState causes.

  useEffect(() => {
    if (!live) return;
    const id = setInterval(async () => {
      try {
        const [m, b] = await Promise.all([
          fetch(`/api/proxy/projects/${project}/metrics?window=${selectedWindow}`).then((r) =>
            r.json(),
          ),
          fetch(
            `/api/proxy/projects/${project}/metrics/breakdown?window=${selectedWindow}&dimension=model`,
          ).then((r) => r.json()),
        ]);
        setMetrics(m);
        setBreakdown(b);
      } catch {
        // A failed poll leaves the last good data on screen. Blanking the
        // dashboard because one request timed out is worse than slightly stale
        // numbers — especially during the incident you opened it for.
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [live, project, selectedWindow]);

  const { summary, series } = metrics;
  const labels = series.map((p) => p.bucket);

  const latencySeries = [
    { name: "p50", points: series.map((p, i) => ({ x: i, y: p.p50_latency_ms })), color: SERIES[0] },
    { name: "p95", points: series.map((p, i) => ({ x: i, y: p.p95_latency_ms })), color: SERIES[1] },
    { name: "p99", points: series.map((p, i) => ({ x: i, y: p.p99_latency_ms })), color: SERIES[2] },
  ];

  const errorSeries = [
    {
      name: "error rate",
      points: series.map((p, i) => ({ x: i, y: p.error_rate * 100 })),
      color: SERIES[7],
    },
  ];

  const errorTone =
    summary.error_rate > 0.05 ? "critical" : summary.error_rate > 0.01 ? "warning" : "good";

  return (
    <>
      {/* Filters sit in one row above the charts, not scattered per-card. */}
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <div
          className="inline-flex rounded-md p-0.5"
          style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
          role="group"
          aria-label="Time window"
        >
          {WINDOWS.map((w) => (
            <button
              key={w}
              onClick={() => router.push(`/${project}?window=${w}`)}
              className="rounded px-2.5 py-1 text-xs font-medium transition-colors"
              style={{
                background: w === selectedWindow ? "var(--accent-wash)" : "transparent",
                color: w === selectedWindow ? "var(--accent)" : "var(--ink-secondary)",
              }}
              aria-pressed={w === selectedWindow}
            >
              {w}
            </button>
          ))}
        </div>

        <button
          onClick={() => setLive((v) => !v)}
          className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            color: live ? "var(--success-text)" : "var(--ink-muted)",
          }}
        >
          <span
            className="inline-block h-1.5 w-1.5 rounded-full"
            style={{ background: live ? "var(--good)" : "var(--ink-muted)" }}
            aria-hidden
          />
          {live ? "Live" : "Paused"}
        </button>
      </div>

      {/* Summary before detail — the tiles answer "is anything wrong" at a glance. */}
      <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Traces"
          value={formatCompact(summary.trace_count)}
          sub={`${formatCompact(summary.span_count)} spans`}
        />
        <Stat
          label="Error rate"
          value={formatPercent(summary.error_rate)}
          sub={`${summary.error_count} errored spans`}
          tone={errorTone}
        />
        <Stat
          label="p95 latency"
          value={formatMs(summary.p95_latency_ms)}
          sub={`p50 ${formatMs(summary.p50_latency_ms)} · p99 ${formatMs(summary.p99_latency_ms)}`}
        />
        <Stat
          label="Cost"
          value={formatUsd(summary.cost_usd)}
          sub={`${formatCompact(summary.prompt_tokens + summary.completion_tokens)} tokens`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardTitle hint={`${metrics.bucket} buckets`}>Latency</CardTitle>
          {/* Three percentiles on one axis. Never a second y-scale — that is
              the single most common charting mistake, and it makes any two
              series look correlated by construction. */}
          <LineChart series={latencySeries} labels={labels} unit="ms" formatValue={formatMs} />
          <div className="mt-2">
            <Legend series={latencySeries} />
          </div>
        </Card>

        <Card>
          <CardTitle hint={`${metrics.bucket} buckets`}>Request volume</CardTitle>
          <BarChart values={series.map((p) => p.span_count)} labels={labels} />
        </Card>

        <Card>
          <CardTitle hint="percent of spans">Error rate</CardTitle>
          <LineChart
            series={errorSeries}
            labels={labels}
            unit="%"
            formatValue={(v) => `${v.toFixed(1)}`}
          />
        </Card>

        <Card>
          <CardTitle hint="USD per bucket">Cost</CardTitle>
          <BarChart
            values={series.map((p) => p.cost_usd)}
            labels={labels}
            color="var(--series-3)"
            formatValue={(v) => (v < 1 ? v.toFixed(3) : v.toFixed(2))}
          />
        </Card>
      </div>

      <div className="mt-4">
        <CardTitle hint="this window">By model</CardTitle>
        {breakdown.length === 0 ? (
          <Card>
            <p className="text-sm" style={{ color: "var(--ink-muted)" }}>
              No model calls recorded in this window.
            </p>
          </Card>
        ) : (
          <TableShell>
            <thead>
              <tr>
                <Th>Model</Th>
                <Th align="right">Calls</Th>
                <Th align="right">Errors</Th>
                <Th align="right">p95</Th>
                <Th align="right">Cost</Th>
              </tr>
            </thead>
            <tbody>
              {breakdown.map((row) => (
                <tr key={row.label}>
                  <Td>{row.label}</Td>
                  <Td align="right" mono>
                    {formatCompact(row.span_count)}
                  </Td>
                  <Td align="right" mono>
                    <span style={{ color: row.error_count ? "var(--critical)" : undefined }}>
                      {row.error_count}
                    </span>
                  </Td>
                  <Td align="right" mono>
                    {formatMs(row.p95_latency_ms)}
                  </Td>
                  <Td align="right" mono>
                    {formatUsd(row.cost_usd)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        )}
      </div>
    </>
  );
}
