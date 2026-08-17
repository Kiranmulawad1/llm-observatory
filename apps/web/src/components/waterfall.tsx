"use client";

import { useState } from "react";
import type { SpanNode } from "@/lib/api";
import { KindTag, Mono, StatusPill, formatMs, formatUsd } from "@/components/ui";

/**
 * Span waterfall.
 *
 * The form is chosen by the question it answers: "where did the time go?" A
 * table of durations makes you compare numbers; a waterfall makes overlap and
 * sequencing visible, so a retrieval that blocks generation looks different
 * from one that runs alongside it.
 *
 * Bar colour encodes span *kind*, using the categorical slots in fixed order —
 * so the same kind is the same colour in every trace, and colour follows the
 * entity rather than its position in this particular list.
 */

const KIND_COLOR: Record<string, string> = {
  chain: "var(--series-1)",
  llm: "var(--series-2)",
  retrieval: "var(--series-3)",
  rerank: "var(--series-4)",
  tool: "var(--series-5)",
  embedding: "var(--series-6)",
  guardrail: "var(--series-7)",
  other: "var(--ink-muted)",
};

export function Waterfall({
  rows,
  totalMs,
}: {
  rows: { span: SpanNode; depth: number }[];
  totalMs: number;
}) {
  const [open, setOpen] = useState<string | null>(null);

  if (rows.length === 0) {
    return (
      <p className="text-sm" style={{ color: "var(--ink-muted)" }}>
        This trace has no spans.
      </p>
    );
  }

  const start = Math.min(...rows.map((r) => new Date(r.span.started_at).getTime()));
  const span = Math.max(totalMs, 1);

  return (
    <div
      className="overflow-hidden rounded-lg"
      style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
    >
      {rows.map(({ span: s, depth }) => {
        const offset = new Date(s.started_at).getTime() - start;
        const width = ((s.duration_ms ?? 0) / span) * 100;
        const left = (offset / span) * 100;
        const isOpen = open === s.span_id;
        const color = s.status === "error" ? "var(--critical)" : KIND_COLOR[s.kind] ?? KIND_COLOR.other;

        return (
          <div key={s.span_id} style={{ borderTop: "1px solid var(--border)" }}>
            <button
              onClick={() => setOpen(isOpen ? null : s.span_id)}
              className="flex w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-[var(--accent-wash)]"
              aria-expanded={isOpen}
            >
              <span
                className="flex min-w-0 shrink-0 items-center gap-2"
                style={{ width: 280, paddingLeft: depth * 16 }}
              >
                <span
                  className="shrink-0 text-xs"
                  style={{ color: "var(--ink-muted)", width: 10 }}
                  aria-hidden
                >
                  {isOpen ? "▾" : "▸"}
                </span>
                <span
                  className="truncate text-sm"
                  style={{ color: "var(--ink)", fontWeight: depth === 0 ? 600 : 400 }}
                  title={s.name}
                >
                  {s.name}
                </span>
                <KindTag kind={s.kind} />
              </span>

              {/* The bar track. Position encodes when, length encodes how long. */}
              <span className="relative h-4 min-w-0 flex-1" style={{ background: "var(--grid)" }}>
                <span
                  className="absolute top-0 h-full"
                  style={{
                    left: `${Math.max(0, Math.min(100, left))}%`,
                    width: `${Math.max(0.6, Math.min(100 - left, width))}%`,
                    background: color,
                    borderRadius: 2,
                  }}
                />
              </span>

              <span
                className="tabular w-20 shrink-0 text-right text-xs"
                style={{ color: "var(--ink-secondary)" }}
              >
                {formatMs(s.duration_ms)}
              </span>
            </button>

            {isOpen && (
              <div
                className="px-4 pb-4 pl-12 text-sm"
                style={{ background: "var(--page)", color: "var(--ink-secondary)" }}
              >
                <dl className="grid gap-x-6 gap-y-1 py-3 sm:grid-cols-2">
                  <Row label="Span id">
                    <Mono>{s.span_id}</Mono>
                  </Row>
                  <Row label="Status">
                    <StatusPill status={s.status} />
                  </Row>
                  {s.model && <Row label="Model">{s.model}</Row>}
                  {s.prompt_tokens !== null && (
                    <Row label="Tokens">
                      {s.prompt_tokens} in · {s.completion_tokens} out
                    </Row>
                  )}
                  {s.cost_usd && <Row label="Cost">{formatUsd(s.cost_usd)}</Row>}
                </dl>

                {s.error_message && (
                  <Block label="Error" tone="critical">
                    {s.error_type}: {s.error_message}
                  </Block>
                )}
                {s.input && <Block label="Input">{JSON.stringify(s.input, null, 2)}</Block>}
                {s.output && <Block label="Output">{JSON.stringify(s.output, null, 2)}</Block>}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2">
      <dt className="shrink-0" style={{ color: "var(--ink-muted)" }}>
        {label}
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

function Block({
  label,
  children,
  tone,
}: {
  label: string;
  children: React.ReactNode;
  tone?: "critical";
}) {
  return (
    <div className="mt-2">
      <div className="mb-1 text-xs font-medium" style={{ color: "var(--ink-muted)" }}>
        {label}
      </div>
      <pre
        className="scroll-x max-h-64 overflow-y-auto rounded-md p-3 text-xs"
        style={{
          background: "var(--surface)",
          border: `1px solid ${tone === "critical" ? "var(--critical)" : "var(--border)"}`,
          color: tone === "critical" ? "var(--critical)" : "var(--ink-secondary)",
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        }}
      >
        {children}
      </pre>
    </div>
  );
}
