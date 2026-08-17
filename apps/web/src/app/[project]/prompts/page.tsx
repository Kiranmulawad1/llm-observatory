import Link from "next/link";
import { api, type Prompt } from "@/lib/api";
import {
  EmptyState, PageHeader, TableShell, Td, Th, relativeTime,
} from "@/components/ui";

export default async function PromptsPage({
  params,
}: {
  params: Promise<{ project: string }>;
}) {
  const { project } = await params;

  let prompts: Prompt[] = [];
  let error: string | null = null;
  try {
    prompts = await api.get<Prompt[]>(`/projects/${project}/prompts?limit=200`);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load prompts";
  }

  // Judges are prompts too, but they measure rather than run — separating them
  // keeps the registry readable without needing a second table.
  const application = prompts.filter((p) => p.kind === "application");
  const judges = prompts.filter((p) => p.kind === "judge");

  return (
    <>
      <PageHeader
        title="Prompts"
        description="Versions are immutable; labels move. An eval run pins the exact version it tested."
      />

      {error && <EmptyState title="Can't load prompts" hint={error} />}
      {!error && prompts.length === 0 && (
        <EmptyState title="No prompts yet" hint="POST /projects/{slug}/prompts to register one." />
      )}

      {application.length > 0 && <PromptTable project={project} prompts={application} />}

      {judges.length > 0 && (
        <div className="mt-8">
          <h2 className="mb-3 text-sm font-semibold" style={{ color: "var(--ink)" }}>
            Judge rubrics
          </h2>
          <p className="mb-3 text-sm" style={{ color: "var(--ink-secondary)" }}>
            Scoring rubrics, versioned like any other prompt. Editing one changes every score it
            produces — which is why a run records the rubric version it used.
          </p>
          <PromptTable project={project} prompts={judges} />
        </div>
      )}
    </>
  );
}

function PromptTable({ project, prompts }: { project: string; prompts: Prompt[] }) {
  return (
    <TableShell>
      <thead>
        <tr>
          <Th>Prompt</Th>
          <Th align="right">Versions</Th>
          <Th>Labels</Th>
          <Th align="right">Updated</Th>
        </tr>
      </thead>
      <tbody>
        {prompts.map((p) => (
          <tr key={p.id}>
            <Td>
              <Link
                href={`/${project}/prompts/${p.slug}`}
                className="font-medium hover:underline"
                style={{ color: "var(--accent)" }}
              >
                {p.name}
              </Link>
              <div className="text-xs" style={{ color: "var(--ink-muted)" }}>
                {p.slug}
              </div>
            </Td>
            <Td align="right" mono>
              {p.latest_version ?? "—"}
            </Td>
            <Td>
              <div className="flex flex-wrap gap-1">
                {p.labels.length === 0 && (
                  <span className="text-xs" style={{ color: "var(--ink-muted)" }}>
                    none
                  </span>
                )}
                {p.labels.map((l) => (
                  <span
                    key={l.label}
                    className="rounded px-1.5 py-0.5 text-xs"
                    style={{ background: "var(--accent-wash)", color: "var(--accent)" }}
                  >
                    {l.label} → v{l.version}
                  </span>
                ))}
              </div>
            </Td>
            <Td align="right" mono>
              {relativeTime(p.updated_at)}
            </Td>
          </tr>
        ))}
      </tbody>
    </TableShell>
  );
}
