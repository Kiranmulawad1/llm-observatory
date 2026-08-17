import Link from "next/link";
import { api, type Trace } from "@/lib/api";
import {
  EmptyState,
  PageHeader,
  StatusPill,
  TableShell,
  Td,
  Th,
  formatMs,
  formatUsd,
  relativeTime,
} from "@/components/ui";

export default async function TracesPage({
  params,
  searchParams,
}: {
  params: Promise<{ project: string }>;
  searchParams: Promise<{ status?: string }>;
}) {
  const { project } = await params;
  const { status } = await searchParams;

  let traces: Trace[] = [];
  let error: string | null = null;

  try {
    const query = status ? `?status=${status}&limit=100` : "?limit=100";
    traces = await api.get<Trace[]>(`/projects/${project}/traces${query}`);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load traces";
  }

  return (
    <>
      <PageHeader
        title="Traces"
        description="Production requests from instrumented applications. Last 24 hours."
        actions={
          <div
            className="inline-flex rounded-md p-0.5"
            style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
          >
            {[
              { value: undefined, label: "All" },
              { value: "error", label: "Errors" },
              { value: "ok", label: "OK" },
            ].map((f) => (
              <Link
                key={f.label}
                href={f.value ? `/${project}/traces?status=${f.value}` : `/${project}/traces`}
                className="rounded px-2.5 py-1 text-xs font-medium"
                style={{
                  background: status === f.value ? "var(--accent-wash)" : "transparent",
                  color: status === f.value ? "var(--accent)" : "var(--ink-secondary)",
                }}
              >
                {f.label}
              </Link>
            ))}
          </div>
        }
      />

      {error && <EmptyState title="Can't load traces" hint={error} />}

      {!error && traces.length === 0 && (
        <EmptyState
          title="No traces yet"
          hint="Instrument an app with the SDK and send some requests — see the README."
        />
      )}

      {traces.length > 0 && (
        <TableShell>
          <thead>
            <tr>
              <Th>Trace</Th>
              <Th>Status</Th>
              <Th align="right">Duration</Th>
              <Th align="right">Spans</Th>
              <Th align="right">Tokens</Th>
              <Th align="right">Cost</Th>
              <Th align="right">When</Th>
            </tr>
          </thead>
          <tbody>
            {traces.map((t) => (
              <tr key={t.trace_id}>
                <Td>
                  <Link
                    href={`/${project}/traces/${t.trace_id}`}
                    className="font-medium hover:underline"
                    style={{ color: "var(--accent)" }}
                  >
                    {t.name}
                  </Link>
                  <div
                    className="tabular text-xs"
                    style={{ color: "var(--ink-muted)", fontFamily: "ui-monospace, monospace" }}
                  >
                    {t.trace_id.slice(0, 16)}…
                  </div>
                </Td>
                <Td>
                  <StatusPill status={t.status} />
                </Td>
                <Td align="right" mono>
                  {formatMs(t.duration_ms)}
                </Td>
                <Td align="right" mono>
                  {t.span_count}
                  {t.error_count > 0 && (
                    <span style={{ color: "var(--critical)" }}> ({t.error_count} err)</span>
                  )}
                </Td>
                <Td align="right" mono>
                  {t.total_prompt_tokens + t.total_completion_tokens || "—"}
                </Td>
                <Td align="right" mono>
                  {formatUsd(t.total_cost_usd)}
                </Td>
                <Td align="right" mono>
                  {relativeTime(t.started_at)}
                </Td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}
    </>
  );
}
