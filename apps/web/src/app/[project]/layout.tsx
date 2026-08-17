import Link from "next/link";
import { NavLinks } from "@/components/nav";

/**
 * Project shell.
 *
 * A persistent left rail rather than top tabs: this is an operator's tool with
 * six destinations, and a rail keeps them all visible while a table scrolls.
 */
export default async function ProjectLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ project: string }>;
}) {
  const { project } = await params;

  return (
    <div className="flex min-h-screen">
      <aside
        className="w-56 shrink-0 px-3 py-6"
        style={{ borderRight: "1px solid var(--border)", background: "var(--surface)" }}
      >
        <Link href="/" className="mb-6 block px-3">
          <div className="text-sm font-semibold" style={{ color: "var(--ink)" }}>
            llm-observatory
          </div>
          <div className="text-xs" style={{ color: "var(--ink-muted)" }}>
            {project}
          </div>
        </Link>
        <NavLinks project={project} />
      </aside>

      <main className="min-w-0 flex-1 px-8 py-8">{children}</main>
    </div>
  );
}
