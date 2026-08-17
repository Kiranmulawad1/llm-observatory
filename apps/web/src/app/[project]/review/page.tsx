import { api, type Dataset, type ReviewItem, type ReviewStats } from "@/lib/api";
import { ReviewQueue } from "@/components/review-queue";
import { EmptyState, PageHeader, Stat, formatPercent } from "@/components/ui";

/**
 * The review queue — the human half of the data flywheel.
 *
 * Production traffic is sampled, cheap checks flag what looks wrong, a person
 * decides, and the decision becomes an eval example. This page is where the
 * deciding happens.
 */
export default async function ReviewPage({
  params,
  searchParams,
}: {
  params: Promise<{ project: string }>;
  searchParams: Promise<{ status?: string }>;
}) {
  const { project } = await params;
  const { status = "pending" } = await searchParams;

  let items: ReviewItem[] = [];
  let stats: ReviewStats | null = null;
  let datasets: Dataset[] = [];
  let error: string | null = null;

  try {
    [items, stats, datasets] = await Promise.all([
      api.get<ReviewItem[]>(`/projects/${project}/review?status=${status}&limit=100`),
      api.get<ReviewStats>(`/projects/${project}/review/stats`),
      api.get<Dataset[]>(`/projects/${project}/datasets`),
    ]);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load the review queue";
  }

  return (
    <>
      <PageHeader
        title="Review queue"
        description="Sampled production traces that tripped a check, plus a control sample. Labels become eval examples."
      />

      {error && <EmptyState title="Can't load the queue" hint={error} />}

      {stats && (
        <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Pending" value={String(stats.pending)} sub="awaiting a verdict" />
          <Stat
            label="Labelled"
            value={String(stats.labeled)}
            sub={`${stats.promoted} promoted to datasets`}
          />
          <Stat
            label="Control reviewed"
            value={String(stats.control_reviewed)}
            sub="clean traces checked by a human"
          />
          <Stat
            label="Estimated miss rate"
            value={
              stats.estimated_miss_rate === null
                ? "—"
                : formatPercent(stats.estimated_miss_rate)
            }
            sub="clean traces judged bad"
            tone={
              stats.estimated_miss_rate === null
                ? "neutral"
                : stats.estimated_miss_rate > 0.2
                  ? "critical"
                  : stats.estimated_miss_rate > 0.05
                    ? "warning"
                    : "good"
            }
          />
        </div>
      )}

      {stats && stats.control_reviewed > 0 && stats.control_missed > 0 && (
        <div
          className="mb-5 rounded-lg px-4 py-3 text-sm"
          style={{
            background: "rgba(250,178,25,0.10)",
            border: "1px solid var(--warning)",
            color: "var(--ink-secondary)",
          }}
        >
          <strong style={{ color: "var(--ink)" }}>
            {stats.control_missed} of {stats.control_reviewed} control traces were judged bad.
          </strong>{" "}
          Those are failures the automatic checks did not catch — the reason a slice of clean
          traffic is reviewed at all. A rising rate means the heuristics need work.
        </div>
      )}

      {!error && items.length === 0 && (
        <EmptyState
          title={status === "pending" ? "Nothing to review" : "No items with that status"}
          hint={
            status === "pending"
              ? "Enable guardrails in Settings and send some traffic — the sampler runs every five minutes."
              : undefined
          }
        />
      )}

      {items.length > 0 && (
        <ReviewQueue
          project={project}
          items={items}
          datasets={datasets.map((d) => d.slug)}
          status={status}
        />
      )}
    </>
  );
}
