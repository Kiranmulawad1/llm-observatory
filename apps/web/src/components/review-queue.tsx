"use client";

import Link from "next/link";
import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import type { Finding, ReviewItem } from "@/lib/api";
import { labelItem, promoteItems, skipItem } from "@/app/[project]/review/actions";
import { Card, KindTag, relativeTime } from "@/components/ui";

const REASONS = [
  "hallucinated_fact",
  "hallucinated_price",
  "unsupported_claim",
  "wrong_answer",
  "leaked_pii",
  "unhelpful_tone",
  "refused_incorrectly",
  "other",
];

/**
 * The queue.
 *
 * One item expanded at a time, because reviewing is a focused task and a wall
 * of simultaneously-open forms invites careless labelling. Selection for
 * promotion is separate from labelling: you label as you go, then promote a
 * batch — which matters because dataset versions are immutable and one
 * promotion is one version.
 */
export function ReviewQueue({
  project,
  items,
  datasets,
  status,
}: {
  project: string;
  items: ReviewItem[];
  datasets: string[];
  status: string;
}) {
  const router = useRouter();
  const [open, setOpen] = useState<string | null>(items[0]?.id ?? null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const labelled = items.filter((i) => i.status === "labeled" && !i.promoted_at);

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div
          className="inline-flex rounded-md p-0.5"
          style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
        >
          {["pending", "labeled", "skipped"].map((s) => (
            <Link
              key={s}
              href={`/${project}/review?status=${s}`}
              className="rounded px-2.5 py-1 text-xs font-medium capitalize"
              style={{
                background: status === s ? "var(--accent-wash)" : "transparent",
                color: status === s ? "var(--accent)" : "var(--ink-secondary)",
              }}
            >
              {s}
            </Link>
          ))}
        </div>

        {labelled.length > 0 && (
          <form
            action={(formData) =>
              startTransition(async () => {
                selected.forEach((id) => formData.append("item_ids", id));
                const result = await promoteItems(project, formData);
                setMessage(
                  result.error ?? `Promoted ${selected.size} example(s) as version ${result.version}.`,
                );
                if (!result.error) setSelected(new Set());
                router.refresh();
              })
            }
            className="flex items-center gap-2"
          >
            <select
              name="dataset"
              className="rounded px-2 py-1 text-xs"
              style={{
                background: "var(--surface)",
                border: "1px solid var(--border)",
                color: "var(--ink)",
              }}
              aria-label="Dataset to promote into"
            >
              {datasets.length === 0 && <option value="">no datasets</option>}
              {datasets.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
            <button
              type="submit"
              disabled={selected.size === 0 || pending || datasets.length === 0}
              className="rounded-md px-3 py-1.5 text-xs font-medium disabled:opacity-40"
              style={{ background: "var(--accent)", color: "#fff" }}
            >
              Promote {selected.size || ""} to dataset
            </button>
          </form>
        )}
      </div>

      {message && (
        <div
          className="mb-4 rounded-lg px-4 py-2.5 text-sm"
          style={{
            background: "var(--accent-wash)",
            border: "1px solid var(--border)",
            color: "var(--ink-secondary)",
          }}
          role="status"
        >
          {message}
        </div>
      )}

      <div className="flex flex-col gap-2">
        {items.map((item) => (
          <ItemRow
            key={item.id}
            project={project}
            item={item}
            open={open === item.id}
            onToggle={() => setOpen(open === item.id ? null : item.id)}
            selectable={item.status === "labeled" && !item.promoted_at}
            selected={selected.has(item.id)}
            onSelect={() => toggle(item.id)}
            onChanged={() => router.refresh()}
          />
        ))}
      </div>
    </>
  );
}

function ItemRow({
  project,
  item,
  open,
  onToggle,
  selectable,
  selected,
  onSelect,
  onChanged,
}: {
  project: string;
  item: ReviewItem;
  open: boolean;
  onToggle: () => void;
  selectable: boolean;
  selected: boolean;
  onSelect: () => void;
  onChanged: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<string>(item.verdict ?? "");
  const [pending, startTransition] = useTransition();

  return (
    <Card className="p-0">
      <div className="flex items-start gap-3 p-4">
        {selectable && (
          <input
            type="checkbox"
            checked={selected}
            onChange={onSelect}
            className="mt-1"
            aria-label={`Select ${item.trace_name} for promotion`}
          />
        )}

        <button onClick={onToggle} className="min-w-0 flex-1 text-left" aria-expanded={open}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium" style={{ color: "var(--ink)" }}>
              {item.trace_name || "trace"}
            </span>
            {item.sampled_as === "control" ? (
              <KindTag kind="control sample" />
            ) : (
              item.findings.map((f) => <FindingTag key={f.check} finding={f} />)
            )}
            {item.verdict && (
              <span
                className="rounded px-1.5 py-0.5 text-xs font-medium"
                style={{
                  background:
                    item.verdict === "good" ? "rgba(12,163,12,0.12)" : "rgba(208,59,59,0.12)",
                  color: item.verdict === "good" ? "var(--success-text)" : "var(--critical)",
                }}
              >
                {item.verdict}
              </span>
            )}
            {item.promoted_at && <KindTag kind="promoted" />}
          </div>
          <p
            className="mt-1 line-clamp-2 text-sm"
            style={{ color: "var(--ink-secondary)" }}
          >
            {item.output ?? "(no output captured)"}
          </p>
          <div className="mt-1 text-xs" style={{ color: "var(--ink-muted)" }}>
            {relativeTime(item.created_at)} · {item.model ?? "no model"} ·{" "}
            <Link
              href={`/${project}/traces/${item.trace_id}`}
              className="hover:underline"
              style={{ color: "var(--accent)" }}
              onClick={(e) => e.stopPropagation()}
            >
              view trace
            </Link>
          </div>
        </button>
      </div>

      {open && (
        <div
          className="border-t px-4 py-4"
          style={{ borderColor: "var(--border)", background: "var(--page)" }}
        >
          <div className="grid gap-4 lg:grid-cols-2">
            <div>
              <FieldLabel>Input</FieldLabel>
              <Pre>{JSON.stringify(item.inputs, null, 2)}</Pre>

              {item.context && (
                <>
                  <FieldLabel>Retrieved context</FieldLabel>
                  <Pre>
                    {item.context
                      .map((c, i) => `[${i + 1}] ${typeof c === "string" ? c : JSON.stringify(c)}`)
                      .join("\n\n")}
                  </Pre>
                </>
              )}

              <FieldLabel>Output</FieldLabel>
              <Pre>{item.output ?? "(none)"}</Pre>

              {item.findings.length > 0 && (
                <>
                  <FieldLabel>Why it was flagged</FieldLabel>
                  <Pre>{JSON.stringify(item.findings, null, 2)}</Pre>
                </>
              )}
            </div>

            <div>
              {item.promoted_at ? (
                <p className="text-sm" style={{ color: "var(--ink-muted)" }}>
                  Promoted into a dataset {relativeTime(item.promoted_at)}. Labels are frozen
                  once an example is in a dataset version.
                </p>
              ) : (
                <form
                  action={(formData) =>
                    startTransition(async () => {
                      const result = await labelItem(project, item.id, formData);
                      setError(result.error ?? null);
                      if (!result.error) onChanged();
                    })
                  }
                  className="flex flex-col gap-3"
                >
                  <div>
                    <FieldLabel>Verdict</FieldLabel>
                    <div className="flex gap-2">
                      {["good", "bad"].map((v) => (
                        <label
                          key={v}
                          className="flex cursor-pointer items-center gap-1.5 rounded-md px-3 py-1.5 text-sm"
                          style={{
                            background:
                              verdict === v ? "var(--accent-wash)" : "var(--surface)",
                            border: `1px solid ${verdict === v ? "var(--accent)" : "var(--border)"}`,
                          }}
                        >
                          <input
                            type="radio"
                            name="verdict"
                            value={v}
                            checked={verdict === v}
                            onChange={() => setVerdict(v)}
                          />
                          {v}
                        </label>
                      ))}
                    </div>
                  </div>

                  <div>
                    <FieldLabel>Reason</FieldLabel>
                    <select
                      name="reason"
                      defaultValue={item.label_reason ?? ""}
                      className="w-full rounded px-2 py-1.5 text-sm"
                      style={{
                        background: "var(--surface)",
                        border: "1px solid var(--border)",
                        color: "var(--ink)",
                      }}
                    >
                      <option value="">—</option>
                      {REASONS.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div>
                    <FieldLabel>
                      What it should have said
                      {verdict === "bad" && (
                        <span style={{ color: "var(--critical)" }}> · required</span>
                      )}
                    </FieldLabel>
                    <textarea
                      name="corrected_output"
                      defaultValue={item.corrected_output ?? item.output ?? ""}
                      rows={5}
                      className="w-full rounded px-2 py-1.5 text-sm"
                      style={{
                        background: "var(--surface)",
                        border: "1px solid var(--border)",
                        color: "var(--ink)",
                        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                      }}
                    />
                    <p className="mt-1 text-xs" style={{ color: "var(--ink-muted)" }}>
                      This becomes the expected output when the example is promoted.
                    </p>
                  </div>

                  <div>
                    <FieldLabel>Notes</FieldLabel>
                    <input
                      name="notes"
                      defaultValue={item.notes ?? ""}
                      className="w-full rounded px-2 py-1.5 text-sm"
                      style={{
                        background: "var(--surface)",
                        border: "1px solid var(--border)",
                        color: "var(--ink)",
                      }}
                    />
                  </div>

                  <input type="hidden" name="labeled_by" value="dashboard" />

                  {error && (
                    <p className="text-sm" style={{ color: "var(--critical)" }} role="alert">
                      {error}
                    </p>
                  )}

                  <div className="flex gap-2">
                    <button
                      type="submit"
                      disabled={pending}
                      className="rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-40"
                      style={{ background: "var(--accent)", color: "#fff" }}
                    >
                      Save label
                    </button>
                    <button
                      type="button"
                      disabled={pending}
                      onClick={() =>
                        startTransition(async () => {
                          await skipItem(project, item.id);
                          onChanged();
                        })
                      }
                      className="rounded-md px-3 py-1.5 text-sm disabled:opacity-40"
                      style={{
                        background: "var(--surface)",
                        border: "1px solid var(--border)",
                        color: "var(--ink-secondary)",
                      }}
                    >
                      Skip
                    </button>
                  </div>
                  <p className="text-xs" style={{ color: "var(--ink-muted)" }}>
                    Skip dismisses a false positive without recording a judgement.
                  </p>
                </form>
              )}
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

/** Severity drives the colour; the check name carries the meaning. */
function FindingTag({ finding }: { finding: Finding }) {
  const tone =
    finding.severity >= 0.9
      ? { bg: "rgba(208,59,59,0.12)", fg: "var(--critical)" }
      : finding.severity >= 0.6
        ? { bg: "rgba(236,131,90,0.16)", fg: "var(--serious)" }
        : { bg: "rgba(250,178,25,0.16)", fg: "var(--ink-secondary)" };

  return (
    <span
      className="rounded px-1.5 py-0.5 text-xs font-medium"
      style={{ background: tone.bg, color: tone.fg }}
      title={`severity ${finding.severity.toFixed(2)}`}
    >
      {finding.check}
    </span>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="mb-1 mt-3 text-xs font-medium uppercase first:mt-0"
      style={{ color: "var(--ink-muted)", letterSpacing: "0.04em" }}
    >
      {children}
    </div>
  );
}

function Pre({ children }: { children: React.ReactNode }) {
  return (
    <pre
      className="scroll-x max-h-48 overflow-y-auto rounded-md p-2.5 text-xs"
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        color: "var(--ink-secondary)",
        whiteSpace: "pre-wrap",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
      }}
    >
      {children}
    </pre>
  );
}
