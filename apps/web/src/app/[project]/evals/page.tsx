import Link from "next/link";
import { api, type EvalRun } from "@/lib/api";
import {
  EmptyState, PageHeader, StatusPill, TableShell, Td, Th, relativeTime,
} from "@/components/ui";

export default async function EvalsPage({
  params,
}: {
  params: Promise<{ project: string }>;
}) {
  const { project } = await params;

  let runs: EvalRun[] = [];
  let error: string | null = null;
  try {
    runs = await api.get<EvalRun[]>(`/projects/${project}/eval/runs?limit=100`);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load eval runs";
  }

  return (
    <>
      <PageHeader
        title="Eval runs"
        description="Each run pins the exact dataset and prompt version it tested."
        actions={
          runs.length >= 2 && (
            <Link
              href={`/${project}/evals/compare?baseline=${runs[1].id}&candidate=${runs[0].id}`}
              className="rounded-md px-3 py-1.5 text-sm font-medium"
              style={{ background: "var(--accent)", color: "#fff" }}
            >
              Compare latest two
            </Link>
          )
        }
      />

      {error && <EmptyState title="Can't load runs" hint={error} />}
      {!error && runs.length === 0 && (
        <EmptyState
          title="No eval runs yet"
          hint="POST /projects/{slug}/eval/runs to start one."
        />
      )}

      {runs.length > 0 && (
        <TableShell>
          <thead>
            <tr>
              <Th>Run</Th>
              <Th>Status</Th>
              <Th>Evaluators</Th>
              <Th align="right">Progress</Th>
              <Th align="right">Scores</Th>
              <Th align="right">Started</Th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <Td>
                  <Link
                    href={`/${project}/evals/${r.id}`}
                    className="font-medium hover:underline"
                    style={{ color: "var(--accent)" }}
                  >
                    {r.label ?? r.id.slice(0, 8)}
                  </Link>
                  {r.commit_sha && (
                    <div className="text-xs" style={{ color: "var(--ink-muted)" }}>
                      {r.commit_sha.slice(0, 7)}
                    </div>
                  )}
                </Td>
                <Td>
                  <StatusPill status={r.status} />
                </Td>
                <Td>
                  <span className="text-xs" style={{ color: "var(--ink-secondary)" }}>
                    {r.evaluators.map((e) => e.type).join(", ")}
                  </span>
                </Td>
                <Td align="right" mono>
                  {r.completed_items}/{r.total_items}
                  {r.failed_items > 0 && (
                    <span style={{ color: "var(--critical)" }}> ({r.failed_items} failed)</span>
                  )}
                </Td>
                <Td align="right" mono>
                  {Object.values(r.aggregate_scores).length === 0
                    ? "—"
                    : Object.values(r.aggregate_scores)
                        .map((a) => (a.mean === null ? "—" : a.mean.toFixed(2)))
                        .join(" / ")}
                </Td>
                <Td align="right" mono>
                  {relativeTime(r.started_at ?? r.created_at)}
                </Td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}
    </>
  );
}
