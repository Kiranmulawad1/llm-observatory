import Link from "next/link";
import { api, type Project } from "@/lib/api";
import { Card, EmptyState, PageHeader, relativeTime } from "@/components/ui";

/**
 * Project picker.
 *
 * Everything below this is scoped to one project, because a project is the
 * tenancy boundary the whole platform is organised around.
 */
export default async function Home() {
  let projects: Project[] = [];
  let error: string | null = null;

  try {
    projects = await api.get<Project[]>("/projects");
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not reach the API";
  }

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <PageHeader
        title="llm-observatory"
        description="Evaluation and observability for LLM applications."
      />

      {error && (
        <EmptyState
          title="Can't reach the API"
          hint={<>Start it with <code>make api</code>. ({error})</>}
        />
      )}

      {!error && projects.length === 0 && (
        <EmptyState
          title="No projects yet"
          hint="Create one: POST /projects with a slug and a name."
        />
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        {projects.map((p) => (
          <Link key={p.id} href={`/${p.slug}`} className="block">
            <Card className="transition-colors hover:border-[var(--accent)]">
              <div className="font-medium" style={{ color: "var(--ink)" }}>
                {p.name}
              </div>
              <div className="mt-0.5 text-xs" style={{ color: "var(--ink-muted)" }}>
                {p.slug} · created {relativeTime(p.created_at)}
              </div>
              {p.description && (
                <p className="mt-2 text-sm" style={{ color: "var(--ink-secondary)" }}>
                  {p.description}
                </p>
              )}
            </Card>
          </Link>
        ))}
      </div>
    </main>
  );
}
