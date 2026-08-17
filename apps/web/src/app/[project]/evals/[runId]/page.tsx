import { api, type EvalRunDetail } from "@/lib/api";
import {
  Card, CardTitle, EmptyState, PageHeader, Stat, StatusPill, TableShell, Td, Th,
  formatMs, formatPercent, formatUsd,
} from "@/components/ui";

export default async function EvalRunPage({
  params,
}: {
  params: Promise<{ project: string; runId: string }>;
}) {
  const { project, runId } = await params;

  let run: EvalRunDetail | null = null;
  let error: string | null = null;
  try {
    run = await api.get<EvalRunDetail>(`/projects/${project}/eval/runs/${runId}?limit=200`);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load run";
  }

  if (error || !run) {
    return (
      <>
        <PageHeader title="Eval run" back={{ href: `/${project}/evals`, label: "Eval runs" }} />
        <EmptyState title="Can't load this run" hint={error ?? undefined} />
      </>
    );
  }

  const aggregates = Object.values(run.aggregate_scores);

  return (
    <>
      <PageHeader
        title={run.label ?? `Run ${run.id.slice(0, 8)}`}
        description={run.evaluators.map((e) => e.type).join(" · ")}
        back={{ href: `/${project}/evals`, label: "Eval runs" }}
        actions={<StatusPill status={run.status} />}
      />

      {run.error && (
        <div
          className="mb-4 rounded-lg px-4 py-3 text-sm"
          style={{
            background: "rgba(208,59,59,0.10)",
            border: "1px solid var(--critical)",
            color: "var(--critical)",
          }}
        >
          {run.error}
        </div>
      )}

      <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Examples" value={`${run.completed_items}/${run.total_items}`} />
        <Stat
          label="Failed"
          value={String(run.failed_items)}
          tone={run.failed_items ? "critical" : "neutral"}
        />
        {aggregates.slice(0, 2).map((a) => (
          <Stat
            key={a.evaluator}
            label={a.evaluator}
            value={a.mean === null ? "—" : a.mean.toFixed(3)}
            sub={
              a.unscoreable
                ? `${a.count} scored · ${a.unscoreable} unscoreable`
                : `${a.count} scored`
            }
          />
        ))}
      </div>

      {aggregates.length > 0 && (
        <div className="mb-5">
          <CardTitle>Scores by evaluator</CardTitle>
          <TableShell>
            <thead>
              <tr>
                <Th>Evaluator</Th>
                <Th align="right">Mean</Th>
                <Th align="right">Min</Th>
                <Th align="right">Max</Th>
                <Th align="right">Pass rate</Th>
                <Th align="right">Scored</Th>
                <Th align="right">Unscoreable</Th>
              </tr>
            </thead>
            <tbody>
              {aggregates.map((a) => (
                <tr key={a.evaluator}>
                  <Td>{a.evaluator}</Td>
                  <Td align="right" mono>{a.mean?.toFixed(3) ?? "—"}</Td>
                  <Td align="right" mono>{a.minimum?.toFixed(3) ?? "—"}</Td>
                  <Td align="right" mono>{a.maximum?.toFixed(3) ?? "—"}</Td>
                  <Td align="right" mono>{formatPercent(a.pass_rate)}</Td>
                  <Td align="right" mono>{a.count}</Td>
                  <Td align="right" mono>
                    {/* Called out separately: an unscoreable example is a
                        dataset gap, not a bad answer, and folding it into the
                        mean as a zero would read as a quality drop. */}
                    <span style={{ color: a.unscoreable ? "var(--warning)" : undefined }}>
                      {a.unscoreable}
                    </span>
                  </Td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        </div>
      )}

      <CardTitle hint={`${run.results.length} shown`}>Per-example results</CardTitle>
      {run.results.length === 0 ? (
        <Card>
          <p className="text-sm" style={{ color: "var(--ink-muted)" }}>
            No results recorded yet.
          </p>
        </Card>
      ) : (
        <TableShell>
          <thead>
            <tr>
              <Th align="right">#</Th>
              <Th>Output</Th>
              <Th>Scores</Th>
              <Th align="right">Latency</Th>
              <Th align="right">Cost</Th>
            </tr>
          </thead>
          <tbody>
            {run.results.map((r) => (
              <tr key={r.id}>
                <Td align="right" mono>{r.item_index}</Td>
                <Td>
                  {r.error ? (
                    <span style={{ color: "var(--critical)" }}>{r.error}</span>
                  ) : (
                    <span
                      className="line-clamp-2 block max-w-md text-xs"
                      style={{ color: "var(--ink-secondary)" }}
                    >
                      {r.output}
                    </span>
                  )}
                </Td>
                <Td>
                  <div className="flex flex-wrap gap-1">
                    {r.scores.map((s) => (
                      <span
                        key={s.evaluator}
                        className="rounded px-1.5 py-0.5 text-xs"
                        style={{
                          background:
                            s.score === null
                              ? "rgba(137,135,129,0.16)"
                              : s.score >= 0.8
                                ? "rgba(12,163,12,0.12)"
                                : s.score >= 0.5
                                  ? "rgba(250,178,25,0.16)"
                                  : "rgba(208,59,59,0.12)",
                          color:
                            s.score === null
                              ? "var(--ink-muted)"
                              : s.score >= 0.8
                                ? "var(--success-text)"
                                : s.score >= 0.5
                                  ? "var(--serious)"
                                  : "var(--critical)",
                        }}
                        title={s.error ?? undefined}
                      >
                        {s.evaluator} {s.score === null ? "n/a" : s.score.toFixed(2)}
                      </span>
                    ))}
                  </div>
                </Td>
                <Td align="right" mono>{formatMs(r.latency_ms)}</Td>
                <Td align="right" mono>{formatUsd(r.cost_usd)}</Td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}
    </>
  );
}
