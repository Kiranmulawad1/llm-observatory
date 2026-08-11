import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emits .next/standalone with only the traced runtime dependencies, which
  // takes the production image from ~1.2GB (full node_modules) to ~200MB.
  output: "standalone",
  // The workspace root is the repo, not apps/web — tell file tracing that so it
  // does not walk the whole monorepo looking for dependencies.
  outputFileTracingRoot: __dirname,
  reactStrictMode: true,
  // Never ship a build that does not typecheck. This is already the default;
  // pinned explicitly so nobody "unblocks the build" by flipping it.
  // (Next 16 removed the `eslint` key — linting runs as its own CI step.)
  typescript: { ignoreBuildErrors: false },
};

export default nextConfig;
