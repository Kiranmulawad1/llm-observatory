# ADR 0005 — Eval engine: execution model, evaluator plugins, providers

**Status:** Accepted (Phase 3)

## Context

An eval run is *N* dataset examples × *M* evaluators, where nearly every unit of
work is an outbound network call. ADR 0002 already chose Arq for that fan-out.
This ADR covers what runs *inside* the job: how examples are executed, how
evaluators are pluggable, and how vendor calls are abstracted.

Three requirements drive the design:

1. A run must be **reproducible** — "run 41 scored worse than run 38" is only
   meaningful if you can say exactly what differed.
2. A run must **survive partial failure** — one provider timeout must not
   discard 499 good results.
3. The engine must be **testable without a model** — an eval platform whose own
   tests are non-deterministic and billed is one nobody runs in CI.

## Decision

### Datasets are versioned exactly like prompts

`datasets → dataset_versions → dataset_items`, immutable, same row-locked
numbering as ADR 0004. A run pins `(dataset_version_id, prompt_version_id)`.

Reusing the pattern is the point: reproducibility needs *both* sides pinned, and
a mutable dataset with an immutable prompt would still let history rewrite
itself.

Items carry `inputs` as a JSON **object**, not a string, because a prompt version
is a template. A flat-string dataset only works for single-variable prompts and
breaks the moment a RAG prompt needs both a question and a context.

### One job per run, bounded internal concurrency

Not one job per example. Per-example fan-out parallelises better and gives
per-example retry, but it needs a completion barrier to know when to aggregate,
and it makes progress reporting a separate accounting problem.

Instead: one job, `asyncio.Semaphore`-bounded concurrency, and per-example result
rows written **incrementally**. That gives live progress for free (count the
rows) and makes the run resumable.

The semaphore is not optional. An unbounded `gather` over 500 examples opens 500
simultaneous provider connections and trips a rate limit — converting a slow run
into a failed one.

**Each concurrent example uses its own database session.** A SQLAlchemy
`AsyncSession` is not safe for concurrent use; sharing one across gathered tasks
corrupts its state in ways that surface far from the cause.

### Resume is keyed on a unique constraint, not a checkpoint

`(eval_run_id, dataset_item_id)` is unique, and results are written with
`INSERT … ON CONFLICT DO UPDATE`. A retried run loads the set of already-completed
examples and skips them.

Errored rows are deliberately **not** treated as complete, so a retry re-attempts
them — a transient timeout should be retried, not frozen into the run's history.

### `partial` is a terminal state

`succeeded | partial | failed | cancelled`. A run where 3 of 500 examples errored
is neither a success nor a failure: collapsing it into "succeeded" hides a
provider outage, and into "failed" discards 497 usable results.

### Scores are normalised to 0.0–1.0, and nullable

Every evaluator returns 0.0–1.0, which is what lets a boolean match, a cosine
similarity and (Phase 4) a judge's rubric rating share one storage column, one
aggregation query and one comparison view.

`score` is **nullable**, paired with an `error`, to distinguish *scored badly*
from *could not be scored*. An exact-match evaluator against an item with no
expected output is unscoreable; recording it as 0.0 would drag the run's mean
down and make a dataset gap look like a quality regression. SQL aggregates skip
NULLs, so the mean stays honest and the gap is counted separately.

### Evaluators: a registry, not entry points

A `@register` decorator populating a dict. Entry-point discovery is the right
answer when third parties ship evaluators as separate distributions, and it is
the documented upgrade path — but with one in-repo consumer it would make
evaluators invisible to static analysis and turn the plugin surface into a public
API before anyone has asked for one.

Each evaluator declares a Pydantic config model, so an invalid configuration is
rejected when the run is **requested** rather than discovered on example 300 of
500, after the provider has already been paid.

### Providers are an interface with a deterministic fake

The engine never imports a vendor SDK. It asks a `GenerationProvider` for text
and an `EmbeddingProvider` for vectors.

The fake provider is not a testing shortcut — it is what makes the engine
testable at all. Real-model tests would be non-deterministic, slow and billed on
every CI run. The fake is hash-derived, so the same input always yields the same
output, and it echoes its input so tests can configure *real* evaluators against
a predictable answer.

Embeddings default to a **local ONNX model** rather than a hosted API: cloning
this repo and running a full eval needs no key and costs nothing, and CI
exercises the real embedding path. The cost is ~150–250 MB of image size and a
few hundred MB of worker memory, which is why `fastembed` is an optional extra
only the worker installs.

### Cost is computed at write time from a dated table

`eval_results.cost_usd` is a durable record of what a run cost. Recomputing
historical rows against today's rates would silently rewrite history the first
time a price changed.

The tradeoff is that the pricing table goes stale, so it carries the date it was
last verified and the API returns an explicit `pricing_stale` flag rather than
presenting a possibly-wrong number as exact. An unpriced model yields `NULL`, not
`0` — unknown cost should be visibly incomplete, not silently wrong.

### Model/parameter compatibility is validated at run request

ADR 0004 stores decoding parameters *with* the prompt version. Current Anthropic
models **reject** `temperature`, `top_p` and `top_k` with a 400. So a perfectly
valid stored prompt plus a newer model is a guaranteed failure — once per
example, mid-run.

The provider layer therefore validates the model/parameter pair when the run is
created, turning N mid-run 400s into one clear 422 on one API call. This is a
direct consequence of two earlier decisions colliding, and the check is the seam
where they are reconciled.

## Consequences

- **We own the dead-letter path.** ADR 0002 accepted this. A job exhausting its
  retries writes `control.dead_letter_jobs` with the payload, the exception and a
  link to the run — diagnosable and replayable, which a broker-level DLQ is not.
- **Runner tests must commit.** The runner opens its own sessions, so it cannot
  see rows in a test's uncommitted transaction. Those tests commit for real and
  clean up by deleting the project; the RESTRICT foreign keys force the cleanup
  into dependency order, which is the constraint working as intended.
- **Per-example fan-out is the scale answer.** At datasets of tens of thousands,
  one job per run becomes a long-running task that a worker restart re-does from
  its last resume point. The fix is chunked fan-out with an aggregation step —
  additive, because progress and resume are already row-based.
- **Aggregates are computed once, at terminal state.** Recomputing on every
  dashboard read would aggregate thousands of score rows per page view. The cost
  is that a mid-flight run shows no aggregate.
- **`eval_run_id` is denormalised onto `eval_scores`.** Aggregation is one
  indexed `GROUP BY` instead of a join back through `eval_results`.
