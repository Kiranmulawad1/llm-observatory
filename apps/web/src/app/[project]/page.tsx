import { api, type BreakdownRow, type MetricsResponse } from "@/lib/api";
import { MetricsView } from "@/components/metrics-view";
import { EmptyState, PageHeader } from "@/components/ui";

/**
 * The observability dashboard.
 *
 * Server-rendered for the first paint, then a client component polls for
 * updates — so the page is useful immediately rather than showing a spinner
 * while the browser fetches.
 */
export default async function Overview({
  params,
  searchParams,
}: {
  params: Promise<{ project: string }>;
  searchParams: Promise<{ window?: string }>;
}) {
  const { project } = await params;
  const { window = "1h" } = await searchParams;

  let metrics: MetricsResponse | null = null;
  let breakdown: BreakdownRow[] = [];
  let error: string | null = null;

  try {
    [metrics, breakdown] = await Promise.all([
      api.get<MetricsResponse>(`/projects/${project}/metrics?window=${window}`),
      api.get<BreakdownRow[]>(
        `/projects/${project}/metrics/breakdown?window=${window}&dimension=model`,
      ),
    ]);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load metrics";
  }

  return (
    <>
      <PageHeader
        title="Overview"
        description="Request volume, latency, errors and cost from production traces."
      />

      {error ? (
        <EmptyState title="Can't load metrics" hint={error} />
      ) : (
        <MetricsView
          // Remount on window change so fresh server props become fresh state.
          key={window}
          project={project}
          initialMetrics={metrics!}
          initialBreakdown={breakdown}
          window={window}
        />
      )}
    </>
  );
}
