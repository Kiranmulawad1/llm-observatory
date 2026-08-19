/**
 * Server-side API client — the BFF boundary.
 *
 * Every function here runs on the Next.js server, never in the browser. That is
 * the whole point of the pattern: the platform credential stays server-side, so
 * a user opening devtools cannot read it and cannot call the API directly with
 * it. The browser talks only to this app's own routes.
 *
 * It is also why the API's CORS is opened for localhost only — in production
 * the browser has no reason to reach the API at all.
 */

import "server-only";

const API_BASE = process.env.LO_API_BASE_URL ?? "http://localhost:8000";
// The platform operator token — the same LO_ADMIN_TOKEN the API validates.
// It lives here, on the server, and never reaches the browser: that is the
// entire point of the BFF boundary (ADR 0008).
const API_KEY = process.env.LO_ADMIN_TOKEN;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "content-type": "application/json",
    ...(init?.headers as Record<string, string>),
  };
  if (API_KEY) headers.authorization = `Bearer ${API_KEY}`;

  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
    // Dashboards show current state; a cached metrics response is a wrong one.
    cache: "no-store",
  });

  if (!response.ok) {
    let code = "error";
    let detail = response.statusText;
    try {
      const body = await response.json();
      code = body.code ?? code;
      detail = body.detail ?? detail;
    } catch {
      // Non-JSON error body — keep the status text.
    }
    throw new ApiError(response.status, code, detail);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

/* --- Types mirroring the API's response models --------------------------- */

export type Project = {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  created_at: string;
};

export type MetricPoint = {
  bucket: string;
  span_count: number;
  error_count: number;
  error_rate: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  p99_latency_ms: number | null;
  cost_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
};

export type MetricsSummary = {
  span_count: number;
  trace_count: number;
  error_count: number;
  error_rate: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  p99_latency_ms: number | null;
  cost_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
};

export type MetricsResponse = {
  window: string;
  bucket: string;
  since: string;
  summary: MetricsSummary;
  series: MetricPoint[];
};

export type BreakdownRow = {
  label: string;
  span_count: number;
  error_count: number;
  error_rate: number;
  p95_latency_ms: number | null;
  cost_usd: number;
};

export type Trace = {
  trace_id: string;
  name: string;
  status: "ok" | "error";
  started_at: string;
  duration_ms: number | null;
  span_count: number;
  error_count: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_cost_usd: string | null;
};

export type SpanNode = {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  status: string;
  started_at: string;
  duration_ms: number | null;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  model: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cost_usd: string | null;
  error_type: string | null;
  error_message: string | null;
  children: SpanNode[];
};

export type TraceDetail = Trace & { root: SpanNode | null; orphans: SpanNode[] };

export type PromptLabel = { label: string; version: number; version_id: string };

export type Prompt = {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  kind: "application" | "judge";
  latest_version: number | null;
  labels: PromptLabel[];
  updated_at: string;
};

export type PromptVersion = {
  id: string;
  version: number;
  messages: { role: string; content: string }[];
  variables: { name: string; required: boolean }[];
  parameters: Record<string, unknown>;
  content_hash: string;
  commit_sha: string | null;
  created_by: string | null;
  change_note: string | null;
  created_at: string;
};

export type MessageDiff = {
  index: number;
  change: "added" | "removed" | "modified" | "unchanged";
  role_from: string | null;
  role_to: string | null;
  content_from: string | null;
  content_to: string | null;
  unified: string;
};

export type PromptDiff = {
  from_version: number;
  to_version: number;
  identical: boolean;
  messages: MessageDiff[];
  parameters: {
    key: string;
    change: string;
    value_from: unknown;
    value_to: unknown;
  }[];
};

export type EvalRun = {
  id: string;
  status: "pending" | "running" | "succeeded" | "partial" | "failed" | "cancelled";
  evaluators: { type: string; config?: Record<string, unknown> }[];
  provider_config: Record<string, unknown>;
  commit_sha: string | null;
  label: string | null;
  total_items: number;
  completed_items: number;
  failed_items: number;
  aggregate_scores: Record<
    string,
    {
      evaluator: string;
      count: number;
      mean: number | null;
      minimum: number | null;
      maximum: number | null;
      pass_rate: number | null;
      unscoreable: number;
    }
  >;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type EvalResult = {
  id: string;
  item_index: number;
  output: string | null;
  error: string | null;
  latency_ms: number | null;
  cost_usd: string | null;
  scores: {
    evaluator: string;
    score: number | null;
    passed: boolean | null;
    detail: Record<string, unknown>;
    error: string | null;
  }[];
};

export type EvalRunDetail = EvalRun & { results: EvalResult[] };

export type ApiKey = {
  id: string;
  name: string;
  key_prefix: string;
  scopes: string[];
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
};

export type AlertRule = {
  id: string;
  name: string;
  metric: string;
  comparison: string;
  threshold: number;
  window_seconds: number;
  cooldown_seconds: number;
  webhook_url: string;
  enabled: boolean;
  last_fired_at: string | null;
  last_value: number | null;
  consecutive_failures: number;
};

export type RunSummary = {
  id: string;
  label: string | null;
  status: string;
  commit_sha: string | null;
  dataset_version_id: string;
  prompt_version_id: string | null;
  created_at: string;
};

export type EvaluatorDelta = {
  evaluator: string;
  baseline_mean: number | null;
  candidate_mean: number | null;
  delta: number | null;
  change: string;
  baseline_pass_rate: number | null;
  candidate_pass_rate: number | null;
};

export type ExampleComparison = {
  item_index: number;
  dataset_item_id: string;
  change: string;
  baseline_output: string | null;
  candidate_output: string | null;
  baseline_scores: Record<string, number | null>;
  candidate_scores: Record<string, number | null>;
  score_deltas: Record<string, number | null>;
  evaluator_changes: Record<string, string>;
};

export type RunComparison = {
  baseline: RunSummary;
  candidate: RunSummary;
  /** "identity" (matched by dataset item) or "positional" (matched by index). */
  alignment: string;
  warnings: string[];
  evaluators: EvaluatorDelta[];
  examples: ExampleComparison[];
  regressed_count: number;
  improved_count: number;
};

export type Finding = {
  check: string;
  severity: number;
  detail: Record<string, unknown>;
};

export type ReviewItem = {
  id: string;
  trace_id: string;
  status: "pending" | "labeled" | "skipped";
  sampled_as: "flagged" | "control";
  findings: Finding[];
  severity: number;
  trace_name: string;
  inputs: Record<string, unknown>;
  output: string | null;
  context: unknown[] | null;
  model: string | null;
  verdict: "good" | "bad" | null;
  label_reason: string | null;
  notes: string | null;
  corrected_output: string | null;
  labeled_by: string | null;
  labeled_at: string | null;
  promoted_at: string | null;
  created_at: string;
};

export type ReviewStats = {
  pending: number;
  labeled: number;
  skipped: number;
  promoted: number;
  control_reviewed: number;
  control_missed: number;
  /** Fraction of "clean" traces a human judged bad — the checks' false-negative rate. */
  estimated_miss_rate: number | null;
};

export type GuardrailConfig = {
  enabled: boolean;
  sample_rate: number;
  control_sample_rate: number;
  check_pii: boolean;
  check_grounding: boolean;
  check_toxicity: boolean;
  escalate_to_judge: boolean;
  last_scanned_at: string | null;
};

export type Dataset = {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  latest_version: number | null;
};
