"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const ITEMS = [
  { href: "", label: "Overview" },
  { href: "/traces", label: "Traces" },
  { href: "/prompts", label: "Prompts" },
  { href: "/evals", label: "Evals" },
  { href: "/review", label: "Review" },
  { href: "/settings", label: "Settings" },
];

export function NavLinks({ project }: { project: string }) {
  const pathname = usePathname();

  return (
    <nav className="flex flex-col gap-0.5">
      {ITEMS.map((item) => {
        const href = `/${project}${item.href}`;
        // Exact match for the overview; prefix match for sections, so a detail
        // page keeps its section highlighted.
        const active = item.href === "" ? pathname === href : pathname.startsWith(href);
        return (
          <Link
            key={item.href}
            href={href}
            className="rounded-md px-3 py-1.5 text-sm transition-colors"
            style={{
              background: active ? "var(--accent-wash)" : "transparent",
              color: active ? "var(--accent)" : "var(--ink-secondary)",
              fontWeight: active ? 600 : 400,
            }}
            aria-current={active ? "page" : undefined}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
