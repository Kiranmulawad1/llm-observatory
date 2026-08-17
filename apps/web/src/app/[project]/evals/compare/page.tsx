import Link from "next/link";
import { api, type RunComparison } from "@/lib/api";
import {
  CardTitle,
  EmptyState,
  PageHeader,
  Stat,
  StatusPill,
  TableShell,
  Td,
  Th,
} from "@/components/ui";

/**
 * Run comparison — the question the whole platform exists to answer.
 *
 * Alignment is the thing to notice. By default examples are matched by
 * `dataset_item_id`, which is exact. Comparing across two dataset versions
 * requires an explicit opt-in, because matching by position after a row was
 * inserted compares unrelated examples and reports regressions that never
 * happened.
 */
export default async function ComparePage({
  params,
  searchParams,
}: {
  params: Promise<{ project: string }>;
  searchParams: Promise<{ baseline?: string; candidate?: string; align?: string }>;
}) {
  const { project } = await params;
  const { baseline, candidate, align } = await searchParams;

  if (!baseline || !candidate) {
    return (
      <>
        <PageHeader title="Compare runs" back={{ href: `/${project}/evals`, label: "Eval runs" }} />
        <EmptyState
          title="Pick two runs"
          hint="Open the eval runs list and choose a baseline and a candidate."
        />
      </>
    );
  }

  let comparison: RunComparison | null = null;
  let error: string | null = null;
  let needsAlignment = false;

  try {
    const query = `baseline=${baseline}&candidate=${candidate}${align ? `&align=${align}` : ""}`;
    comparison = await api.get<RunComparison>(`/projects/${project}/eval/compare?${query}`);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not compare these runs";
    needsAlignment = error.toLowerCase().includes("positional");
  }

  return (
    <>
      <PageHeader
        title="Run comparison"
        description="Did this change make things worse?"
        back={{ href: `/${project}/evals`, label: "Eval runs" }}
      />

      {error && (
        <EmptyState
          title={needsAlignment ? "These runs used different dataset versions" : "Can't compare"}
          hint={
            needsAlignment ? (
              <>
                Examples can only be matched by position, which is unreliable if rows were
                inserted or reordered since the baseline ran.{" "}
                <Link
                  href={`/${project}/evals/compare?baseline=${baseline}&candidate=${candidate}&align=positional`}
                  className="underline"
                  style={{ color: "var(--accent)" }}
                >
                  Compare positionally anyway
                </Link>
              </>
            ) : (
              error
            )
          }
        />
      )}

      {comparison && (
        <>
          {comparison.warnings.length > 0 && (
            <div
              className="mb-4 rounded-lg px-4 py-3 text-sm"
              style={{
                background: "rgba(250,178,25,0.10)",
                border: "1px solid var(--warning)",
                color: "var(--ink-secondary)",
              }}
            >
              {comparison.warnings.map((w) => (
                <p key={w}>{w}</p>
              ))}
            </div>
          )}

          <div className="mb-5 grid gap-3 sm:grid-cols-2">
            <RunCard label="Baseline" run={comparison.baseline} project={project} />
            <RunCard label="Candidate" run={comparison.candidate} project={project} />
          </div>

          <div className="mb-5 grid gap-3 sm:grid-cols-3">
            <Stat
              label="Regressed"
              value={String(comparison.regressed_count)}
              tone={comparison.regressed_count > 0 ? "critical" : "neutral"}
              sub="examples scoring worse"
            />
            <Stat
              label="Improved"
              value={String(comparison.improved_count)}
              tone={comparison.improved_count > 0 ? "good" : "neutral"}
              sub="examples scoring better"
            />
            <Stat
              label="Alignment"
              value={comparison.alignment}
              sub={
                comparison.alignment === "identity"
                  ? "matched by dataset item — exact"
                  : "matched by position — approximate"
              }
              tone={comparison.alignment === "identity" ? "neutral" : "warning"}
            />
          </div>

          <CardTitle>By evaluator</CardTitle>
          <TableShell>
            <thead>
              <tr>
                <Th>Evaluator</Th>
                <Th align="right">Baseline</Th>
                <Th align="right">Candidate</Th>
                <Th align="right">Delta</Th>
                <Th align="right">Pass rate</Th>
              </tr>
            </thead>
            <tbody>
              {comparison.evaluators.map((e) => (
                <tr key={e.evaluator}>
                  <Td>{e.evaluator}</Td>
                  <Td align="right" mono>
                    {e.baseline_mean?.toFixed(3) ?? "—"}
                  </Td>
                  <Td align="right" mono>
                    {e.candidate_mean?.toFixed(3) ?? "—"}
                  </Td>
                  <Td align="right" mono>
                    <Delta delta={e.delta} change={e.change} />
                  </Td>
                  <Td align="right" mono>
                    {pct(e.baseline_pass_rate)} → {pct(e.candidate_pass_rate)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </TableShell>

          {comparison.examples.length > 0 && (
            <div className="mt-5">
              <CardTitle hint={`${comparison.examples.length} changed`}>Changed examples</CardTitle>
              <TableShell>
                <thead>
                  <tr>
                    <Th align="right">#</Th>
                    <Th>Change</Th>
                    <Th>Baseline output</Th>
                    <Th>Candidate output</Th>
                    <Th align="right">Score deltas</Th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.examples.map((x) => (
                    <tr key={x.dataset_item_id}>
                      <Td align="right" mono>
                        {x.item_index}
                      </Td>
                      <Td>
                        <StatusPill
                          status={x.change === "regressed" ? "error" : x.change === "improved" ? "ok" : "cancelled"}
                        />
                      </Td>
                      <Td>
                        <span
                          className="line-clamp-2 block max-w-xs text-xs"
                          style={{ color: "var(--ink-muted)" }}
                        >
                          {x.baseline_output ?? "—"}
                        </span>
                      </Td>
                      <Td>
                        <span
                          className="line-clamp-2 block max-w-xs text-xs"
                          style={{ color: "var(--ink-secondary)" }}
                        >
                          {x.candidate_output ?? "—"}
                        </span>
                      </Td>
                      <Td align="right" mono>
                        <div className="flex flex-col items-end gap-0.5">
                          {Object.entries(x.score_deltas).map(([evaluator, delta]) => (
                            <span key={evaluator} className="text-xs">
                              <span style={{ color: "var(--ink-muted)" }}>{evaluator} </span>
                              <Delta
                                delta={delta}
                                change={x.evaluator_changes?.[evaluator] ?? "unchanged"}
                              />
                            </span>
                          ))}
                        </div>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </TableShell>
            </div>
          )}
        </>
      )}
    </>
  );
}

function RunCard({
  label,
  run,
  project,
}: {
  label: string;
  run: RunComparison["baseline"];
  project: string;
}) {
  return (
    <div
      className="rounded-lg p-4"
      style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
    >
      <div
        className="mb-1 text-xs font-medium uppercase"
        style={{ color: "var(--ink-muted)", letterSpacing: "0.04em" }}
      >
        {label}
      </div>
      <Link
        href={`/${project}/evals/${run.id}`}
        className="font-medium hover:underline"
        style={{ color: "var(--accent)" }}
      >
        {run.label ?? run.id.slice(0, 8)}
      </Link>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
        <StatusPill status={run.status} />
        {run.commit_sha && (
          <span style={{ color: "var(--ink-muted)" }}>{run.commit_sha.slice(0, 7)}</span>
        )}
      </div>
    </div>
  );
}

/** Sign, arrow, and word all carry the direction — never colour alone. */
function Delta({ delta, change }: { delta: number | null; change: string }) {
  if (delta === null || delta === undefined) {
    return <span style={{ color: "var(--ink-muted)" }}>—</span>;
  }
  const color =
    change === "improved"
      ? "var(--success-text)"
      : change === "regressed"
        ? "var(--critical)"
        : "var(--ink-muted)";
  const arrow = change === "improved" ? "▲" : change === "regressed" ? "▼" : "=";
  return (
    <span style={{ color }}>
      {arrow} {delta > 0 ? "+" : ""}
      {delta.toFixed(3)}
    </span>
  );
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(0)}%`;
}
