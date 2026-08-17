"use client";

import { useRouter } from "next/navigation";
import type { PromptDiff } from "@/lib/api";
import { Card } from "@/components/ui";

/**
 * Prompt version diff.
 *
 * The diff itself is computed server-side and arrives structured — this
 * component only renders it. That is deliberate: one diff implementation is
 * shared by this UI, the SDK, and any CI gate, so all three agree on whether
 * something changed.
 *
 * Added and removed lines carry a `+`/`−` prefix as well as colour, so the
 * change is legible in greyscale and to a colourblind reader.
 */
export function DiffView({
  diff,
  project,
  slug,
  versions,
}: {
  diff: PromptDiff;
  project: string;
  slug: string;
  versions: number[];
}) {
  const router = useRouter();

  const navigate = (from: number, to: number) =>
    router.push(`/${project}/prompts/${slug}?from=${from}&to=${to}`);

  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold" style={{ color: "var(--ink)" }}>
          Diff
        </h2>
        <div className="flex items-center gap-2 text-xs">
          <select
            value={diff.from_version}
            onChange={(e) => navigate(Number(e.target.value), diff.to_version)}
            className="rounded px-2 py-1"
            style={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
              color: "var(--ink)",
            }}
            aria-label="Compare from version"
          >
            {versions.map((v) => (
              <option key={v} value={v}>
                v{v}
              </option>
            ))}
          </select>
          <span style={{ color: "var(--ink-muted)" }}>→</span>
          <select
            value={diff.to_version}
            onChange={(e) => navigate(diff.from_version, Number(e.target.value))}
            className="rounded px-2 py-1"
            style={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
              color: "var(--ink)",
            }}
            aria-label="Compare to version"
          >
            {versions.map((v) => (
              <option key={v} value={v}>
                v{v}
              </option>
            ))}
          </select>
        </div>
      </div>

      {diff.identical ? (
        <p className="text-sm" style={{ color: "var(--success-text)" }}>
          These versions are identical.
        </p>
      ) : (
        <>
          <div className="flex flex-col gap-3">
            {diff.messages.map((m) => (
              <div key={m.index}>
                <div className="mb-1 flex items-center gap-2 text-xs">
                  <span
                    className="font-medium uppercase"
                    style={{ color: "var(--ink-muted)", letterSpacing: "0.04em" }}
                  >
                    message {m.index} · {m.role_to ?? m.role_from}
                  </span>
                  <ChangeTag change={m.change} />
                </div>

                {m.change === "unchanged" ? (
                  <p className="text-xs" style={{ color: "var(--ink-muted)" }}>
                    No change.
                  </p>
                ) : (
                  <pre
                    className="scroll-x rounded-md p-3 text-xs leading-relaxed"
                    style={{
                      background: "var(--page)",
                      border: "1px solid var(--border)",
                      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                    }}
                  >
                    {m.unified.split("\n").map((line, i) => {
                      const added = line.startsWith("+") && !line.startsWith("+++");
                      const removed = line.startsWith("-") && !line.startsWith("---");
                      const meta = line.startsWith("@@") || line.startsWith("+++") ||
                        line.startsWith("---");
                      return (
                        <div
                          key={i}
                          style={{
                            color: added
                              ? "var(--success-text)"
                              : removed
                                ? "var(--critical)"
                                : meta
                                  ? "var(--ink-muted)"
                                  : "var(--ink-secondary)",
                            background: added
                              ? "rgba(12,163,12,0.08)"
                              : removed
                                ? "rgba(208,59,59,0.08)"
                                : "transparent",
                          }}
                        >
                          {line || " "}
                        </div>
                      );
                    })}
                  </pre>
                )}
              </div>
            ))}
          </div>

          {/* Parameters get their own section because a temperature change with
              identical text still changes behaviour — and a text-only diff
              would call that "no change". */}
          <div className="mt-4">
            <div
              className="mb-2 text-xs font-medium uppercase"
              style={{ color: "var(--ink-muted)", letterSpacing: "0.04em" }}
            >
              Parameters
            </div>
            <div className="flex flex-col gap-1 text-xs">
              {diff.parameters.map((p) => (
                <div key={p.key} className="flex items-center gap-2">
                  <span style={{ color: "var(--ink-secondary)", minWidth: 120 }}>{p.key}</span>
                  <ChangeTag change={p.change} />
                  <span className="tabular" style={{ color: "var(--ink)" }}>
                    {p.change === "unchanged"
                      ? String(p.value_to)
                      : `${String(p.value_from ?? "—")} → ${String(p.value_to ?? "—")}`}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </Card>
  );
}

function ChangeTag({ change }: { change: string }) {
  const map: Record<string, { fg: string; bg: string; symbol: string }> = {
    added: { fg: "var(--success-text)", bg: "rgba(12,163,12,0.12)", symbol: "+" },
    removed: { fg: "var(--critical)", bg: "rgba(208,59,59,0.12)", symbol: "−" },
    modified: { fg: "var(--serious)", bg: "rgba(236,131,90,0.16)", symbol: "±" },
    unchanged: { fg: "var(--ink-muted)", bg: "transparent", symbol: "=" },
  };
  const s = map[change] ?? map.unchanged;
  return (
    <span
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs"
      style={{ color: s.fg, background: s.bg }}
    >
      <span aria-hidden>{s.symbol}</span>
      {change}
    </span>
  );
}
