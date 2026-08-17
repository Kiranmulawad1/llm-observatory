import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "llm-observatory",
  description: "Evaluation and observability for LLM applications",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body className="min-h-full">{children}</body>
    </html>
  );
}
