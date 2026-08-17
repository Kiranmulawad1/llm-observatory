import { api, type SpanNode, type TraceDetail } from "@/lib/api";
import { Waterfall } from "@/components/waterfall";
import {
  EmptyState,
  PageHeader,
  Stat,
  StatusPill,
  formatMs,
  formatUsd,
} from "@/components/ui";

function flatten(node: SpanNode, depth = 0): { span: SpanNode; depth: number }[] {
  return [{ span: node, depth }, ...node.children.flatMap((c) => flatten(c, depth + 1))];
}

export default async function TraceDetailPage({
  params,
}: {
  params: Promise<{ project: string; traceId: string }>;
}) {
  const { project, traceId } = await params;

  let trace: TraceDetail | null = null;
  let error: string | null = null;

  try {
    trace = await api.get<TraceDetail>(`/projects/${project}/traces/${traceId}`);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load trace";
  }

  if (error || !trace) {
    return (
      <>
        <PageHeader title="Trace" back={{ href: `/${project}/traces`, label: "Traces" }} />
        <EmptyState title="Can't load this trace" hint={error ?? undefined} />
      </>
    );
  }

  const rows = [
    ...(trace.root ? flatten(trace.root) : []),
    // Orphans are shown, not hidden. A span whose parent hasn't arrived yet is
    // real data, and silently dropping it would make a partial trace look
    // complete.
    ...trace.orphans.flatMap((o) => flatten(o)),
  ];

  return (
    <>
      <PageHeader
        title={trace.name}
        description={trace.trace_id}
        back={{ href: `/${project}/traces`, label: "Traces" }}
        actions={<StatusPill status={trace.status} />}
      />

      <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Duration" value={formatMs(trace.duration_ms)} />
        <Stat
          label="Spans"
          value={String(trace.span_count)}
          sub={trace.error_count ? `${trace.error_count} errored` : "no errors"}
          tone={trace.error_count ? "critical" : "neutral"}
        />
        <Stat
          label="Tokens"
          value={String(trace.total_prompt_tokens + trace.total_completion_tokens || 0)}
          sub={`${trace.total_prompt_tokens} in · ${trace.total_completion_tokens} out`}
        />
        <Stat label="Cost" value={formatUsd(trace.total_cost_usd)} />
      </div>

      {trace.orphans.length > 0 && (
        <div
          className="mb-4 rounded-lg px-4 py-3 text-sm"
          style={{
            background: "rgba(250,178,25,0.10)",
            border: "1px solid var(--warning)",
            color: "var(--ink-secondary)",
          }}
        >
          <strong style={{ color: "var(--ink)" }}>
            {trace.orphans.length} span{trace.orphans.length > 1 ? "s" : ""} without a parent.
          </strong>{" "}
          Their parent may still be buffered in the SDK, or a flush was partial. They are shown
          below rather than hidden.
        </div>
      )}

      <Waterfall rows={rows} totalMs={trace.duration_ms ?? 1} />
    </>
  );
}
