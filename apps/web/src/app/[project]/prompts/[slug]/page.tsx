import { api, type Prompt, type PromptDiff, type PromptVersion } from "@/lib/api";
import { DiffView } from "@/components/diff-view";
import { Card, CardTitle, EmptyState, Mono, PageHeader, relativeTime } from "@/components/ui";

export default async function PromptDetail({
  params,
  searchParams,
}: {
  params: Promise<{ project: string; slug: string }>;
  searchParams: Promise<{ from?: string; to?: string }>;
}) {
  const { project, slug } = await params;
  const { from, to } = await searchParams;

  let prompt: Prompt | null = null;
  let versions: PromptVersion[] = [];
  let diff: PromptDiff | null = null;
  let error: string | null = null;

  try {
    [prompt, versions] = await Promise.all([
      api.get<Prompt>(`/projects/${project}/prompts/${slug}`),
      api.get<PromptVersion[]>(`/projects/${project}/prompts/${slug}/versions?limit=50`),
    ]);

    // Default to the two most recent versions — "what changed last?" is the
    // question someone opening this page almost always has.
    const a = from ?? (versions[1]?.version ? String(versions[1].version) : null);
    const b = to ?? (versions[0]?.version ? String(versions[0].version) : null);
    if (a && b && a !== b) {
      diff = await api.get<PromptDiff>(
        `/projects/${project}/prompts/${slug}/diff?from=${a}&to=${b}`,
      );
    }
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load prompt";
  }

  if (error || !prompt) {
    return (
      <>
        <PageHeader title="Prompt" back={{ href: `/${project}/prompts`, label: "Prompts" }} />
        <EmptyState title="Can't load this prompt" hint={error ?? undefined} />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title={prompt.name}
        description={prompt.description ?? prompt.slug}
        back={{ href: `/${project}/prompts`, label: "Prompts" }}
      />

      <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
        <Card>
          <CardTitle hint={`${versions.length}`}>Versions</CardTitle>
          <ol className="flex flex-col gap-1">
            {versions.map((v) => {
              const labels = prompt!.labels.filter((l) => l.version === v.version);
              return (
                <li
                  key={v.id}
                  className="rounded-md px-2 py-1.5 text-sm"
                  style={{ background: "var(--page)" }}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">v{v.version}</span>
                    <span className="text-xs" style={{ color: "var(--ink-muted)" }}>
                      {relativeTime(v.created_at)}
                    </span>
                  </div>
                  {labels.length > 0 && (
                    <div className="mt-1 flex gap-1">
                      {labels.map((l) => (
                        <span
                          key={l.label}
                          className="rounded px-1.5 py-0.5 text-xs"
                          style={{ background: "var(--accent-wash)", color: "var(--accent)" }}
                        >
                          {l.label}
                        </span>
                      ))}
                    </div>
                  )}
                  {v.change_note && (
                    <p className="mt-1 text-xs" style={{ color: "var(--ink-secondary)" }}>
                      {v.change_note}
                    </p>
                  )}
                  {v.commit_sha && (
                    <div className="mt-1">
                      <Mono>{v.commit_sha.slice(0, 7)}</Mono>
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </Card>

        <div className="min-w-0">
          {diff ? (
            <DiffView
              diff={diff}
              project={project}
              slug={slug}
              versions={versions.map((v) => v.version)}
            />
          ) : (
            <Card>
              <CardTitle>Diff</CardTitle>
              <p className="text-sm" style={{ color: "var(--ink-muted)" }}>
                Needs at least two versions to compare.
              </p>
            </Card>
          )}

          {versions[0] && (
            <Card className="mt-4">
              <CardTitle hint={`v${versions[0].version}`}>Latest content</CardTitle>
              <div className="flex flex-col gap-3">
                {versions[0].messages.map((m, i) => (
                  <div key={i}>
                    <div
                      className="mb-1 text-xs font-medium uppercase"
                      style={{ color: "var(--ink-muted)", letterSpacing: "0.04em" }}
                    >
                      {m.role}
                    </div>
                    <pre
                      className="scroll-x rounded-md p-3 text-xs"
                      style={{
                        background: "var(--page)",
                        border: "1px solid var(--border)",
                        color: "var(--ink-secondary)",
                        whiteSpace: "pre-wrap",
                        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                      }}
                    >
                      {m.content}
                    </pre>
                  </div>
                ))}
              </div>
              {versions[0].variables.length > 0 && (
                <div className="mt-3 text-xs" style={{ color: "var(--ink-muted)" }}>
                  Variables: {versions[0].variables.map((v) => v.name).join(", ")}
                </div>
              )}
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
