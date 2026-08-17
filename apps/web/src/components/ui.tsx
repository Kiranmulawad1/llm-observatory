/**
 * Shared building blocks.
 *
 * A dashboard is scanned and operated, not read top to bottom, so state is
 * encoded in *form* as well as number — a status pill, a severity colour, a
 * monospace id — and the summary always precedes the detail.
 */

import Link from "next/link";
import type { ReactNode } from "react";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-lg p-4 ${className}`}
      style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
    >
      {children}
    </section>
  );
}

export function CardTitle({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <header className="mb-3 flex items-baseline justify-between gap-3">
      <h2 className="text-sm font-semibold" style={{ color: "var(--ink)" }}>
        {children}
      </h2>
      {hint && (
        <span className="text-xs" style={{ color: "var(--ink-muted)" }}>
          {hint}
        </span>
      )}
    </header>
  );
}

/**
 * A single headline number.
 *
 * Deliberately not a chart: one value over one window has no shape to show, and
 * a sparkline beside it would be decoration. The label carries the unit so the
 * figure itself stays scannable.
 */
export function Stat({
  label,
  value,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "neutral" | "good" | "warning" | "critical";
}) {
  const toneColor = {
    neutral: "var(--ink)",
    good: "var(--success-text)",
    warning: "var(--warning)",
    critical: "var(--critical)",
  }[tone];

  return (
    <div
      className="rounded-lg px-4 py-3"
      style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
    >
      <div
        className="text-xs font-medium uppercase tracking-wide"
        style={{ color: "var(--ink-muted)", letterSpacing: "0.04em" }}
      >
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold" style={{ color: toneColor }}>
        {value}
      </div>
      {sub && (
        <div className="mt-0.5 text-xs" style={{ color: "var(--ink-muted)" }}>
          {sub}
        </div>
      )}
    </div>
  );
}

/**
 * Status pill.
 *
 * Shape plus text, never colour alone — a colourblind reader and a greyscale
 * print both still read the state.
 */
export function StatusPill({ status }: { status: string }) {
  const map: Record<string, { bg: string; fg: string; label: string }> = {
    ok: { bg: "rgba(12,163,12,0.12)", fg: "var(--success-text)", label: "ok" },
    succeeded: { bg: "rgba(12,163,12,0.12)", fg: "var(--success-text)", label: "succeeded" },
    error: { bg: "rgba(208,59,59,0.12)", fg: "var(--critical)", label: "error" },
    failed: { bg: "rgba(208,59,59,0.12)", fg: "var(--critical)", label: "failed" },
    partial: { bg: "rgba(250,178,25,0.16)", fg: "var(--serious)", label: "partial" },
    running: { bg: "var(--accent-wash)", fg: "var(--accent)", label: "running" },
    pending: { bg: "var(--accent-wash)", fg: "var(--accent)", label: "pending" },
    cancelled: { bg: "rgba(137,135,129,0.16)", fg: "var(--ink-muted)", label: "cancelled" },
  };
  const s = map[status] ?? {
    bg: "rgba(137,135,129,0.16)",
    fg: "var(--ink-secondary)",
    label: status,
  };

  return (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium"
      style={{ background: s.bg, color: s.fg }}
    >
      {s.label}
    </span>
  );
}

export function KindTag({ kind }: { kind: string }) {
  return (
    <span
      className="inline-flex items-center rounded px-1.5 py-0.5 text-xs"
      style={{
        background: "var(--accent-wash)",
        color: "var(--ink-secondary)",
        border: "1px solid var(--border)",
      }}
    >
      {kind}
    </span>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return (
    <code
      className="rounded px-1 py-0.5 text-xs"
      style={{
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        background: "var(--accent-wash)",
        color: "var(--ink-secondary)",
      }}
    >
      {children}
    </code>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div
      className="rounded-lg px-6 py-10 text-center"
      style={{ background: "var(--surface)", border: "1px dashed var(--border-strong)" }}
    >
      <p className="text-sm font-medium" style={{ color: "var(--ink)" }}>
        {title}
      </p>
      {hint && (
        <p className="mt-1 text-xs" style={{ color: "var(--ink-muted)" }}>
          {hint}
        </p>
      )}
    </div>
  );
}

/** Wide tables scroll inside their own container so the page body never does. */
export function TableShell({ children }: { children: ReactNode }) {
  return (
    <div
      className="scroll-x rounded-lg"
      style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
    >
      <table className="w-full text-sm">{children}</table>
    </div>
  );
}

export function Th({ children, align = "left" }: { children: ReactNode; align?: "left" | "right" }) {
  return (
    <th
      className="px-4 py-2.5 text-xs font-medium uppercase"
      style={{
        color: "var(--ink-muted)",
        textAlign: align,
        letterSpacing: "0.04em",
        borderBottom: "1px solid var(--border)",
      }}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  align = "left",
  mono = false,
}: {
  children: ReactNode;
  align?: "left" | "right";
  mono?: boolean;
}) {
  return (
    <td
      className={`px-4 py-2.5 ${mono ? "tabular" : ""}`}
      style={{ textAlign: align, borderBottom: "1px solid var(--border)" }}
    >
      {children}
    </td>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  back,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  back?: { href: string; label: string };
}) {
  return (
    <header className="mb-6">
      {back && (
        <Link
          href={back.href}
          className="mb-2 inline-block text-xs hover:underline"
          style={{ color: "var(--ink-muted)" }}
        >
          ← {back.label}
        </Link>
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold" style={{ color: "var(--ink)" }}>
            {title}
          </h1>
          {description && (
            <p className="mt-1 text-sm" style={{ color: "var(--ink-secondary)" }}>
              {description}
            </p>
          )}
        </div>
        {actions}
      </div>
    </header>
  );
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${Math.round(ms)}ms`;
}

export function formatUsd(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (Number.isNaN(n)) return "—";
  if (n === 0) return "$0";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
