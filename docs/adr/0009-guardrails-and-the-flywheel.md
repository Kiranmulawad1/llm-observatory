# ADR 0009 — Guardrail sampling, the review queue, and the data flywheel

**Status:** Accepted (Phase 7)

## Context

Production surfaces failures the eval set does not contain. Without a path from
"this answer was wrong" back into the dataset, the same failure ships twice and
the eval set stays frozen at whatever someone thought to write on day one.

This phase builds that path:

    production traffic → sampled → checked → flagged → labelled → eval example

## Decision

### Sampling runs on a cron, and is deterministic

**On a worker cron, not at ingest.** Same argument as alerting: running regexes
over model output and string searches over retrieved context on the write path
would put heuristic evaluation inside a customer's request latency. The sampler
reads recent traces afterwards. Every five minutes rather than every minute — a
human reviews the queue hours later, so a wider window does more useful work per
wake-up.

**Deterministic by hash of the trace id**, not random. A 10% rate means "trace
ids whose SHA-256 falls in the first decile." Two properties follow: workers need
no coordination to agree on the sample, and "why wasn't this trace checked?" is
answerable by recomputing the hash instead of shrugging about randomness.

**Errored traces are always sampled** regardless of rate. Sampling away the
failures to hit a quota would be exactly backwards.

**A watermark, not a fixed lookback.** `last_scanned_at` advances to the window
actually examined, so each run starts where the last stopped. Combined with
`ON CONFLICT DO NOTHING` on `(project_id, trace_id)`, a re-run over an
overlapping window never hands a human the same trace twice.

### Three cheap checks, and one of them is the interesting one

Nothing here calls a model. Each returns a severity in 0.0–1.0 plus evidence, so
the queue orders worst-first and a reviewer sees *why* without re-deriving it.

**PII** is regex, and the design work is in the false-positive direction. A
16-digit order number is not a credit card, so matches are validated with a Luhn
checksum; a check that cries wolf gets muted, which is the same as not having it.
Only the **output** is scanned — a user's email in the input is them telling you
their address, while the same email in the output means the model repeated
someone's data back. Matched values are **redacted before storage**, because
writing the raw value into a review item moves the leak from a transient trace
into the control plane, where it lives far longer.

**Grounding** is the hallucination proxy, and it is the one worth explaining.
Without ground truth you cannot detect a fabricated *claim* cheaply — but you can
detect a fabricated *number*. Numbers appearing in the output that appear nowhere
in the retrieved context are the most checkable and most consequential kind of
hallucination, and finding them is a string search. Separators are normalised so
"1,000" matches "1000", common small numbers are ignored, and the check returns
nothing at all when there is no context — it is meaningless for a non-RAG trace,
and flagging every one would drown the queue.

**Toxicity** is a wordlist, and the ADR states plainly what that buys: overt
abuse and slurs, nothing subtler. It cannot detect condescension or a
technically-polite refusal that reads as contempt. It exists because it is free.
Judge escalation is the answer when a project needs better, which is exactly what
the opt-in is for.

**Severity is ordered across checks, not within them.** A leaked API key is 1.0;
the most egregious grounding failure caps at 0.8. A leaked credential is an
incident, an ungrounded number is a quality problem, and the queue ordering
should reflect that.

### Judge escalation is opt-in per project

Off by default, so the whole flywheel costs nothing to run. A project that wants
fewer false positives in its queue can turn on judge confirmation for flagged
items only. Escalating everything would make the sampler require an API key to
function and its tests require a mock.

### The queue takes flagged traces *plus a control sample*

This is the decision that makes the system measurable rather than merely
functional.

Reviewing only flagged traces means only ever seeing failures the checks already
know how to find — a blind spot is invisible **by construction**. So a small
fraction of *clean* sampled traces enters the queue too, unflagged.

`estimated_miss_rate` — the share of control traces a human judged bad — is the
false-negative rate of the heuristics, and the only reason the control sample
exists. A rising value means the checks need work. The control hash is salted
separately (`control:{trace_id}`) so control selection is not correlated with the
sampling decision that preceded it.

### Review items snapshot the trace

`telemetry` is append-only time-series under a retention policy; spans get
dropped. A review item is control-plane data that must **outlive** them: a
labelled example is worth more the older it gets, and a queue full of rows
pointing at deleted traces would be worthless. The item copies input, output,
context and model at sampling time. `trace_id` is kept for linking back while the
trace still exists, deliberately without a foreign key — telemetry may move to
its own instance (ADR 0003), where one could not exist.

### A "bad" verdict requires a correction

Without it, a bad label produces an eval example with no expected output —
unscoreable by exactly the evaluators you would want to run against it. Promotion
rejects it, and the UI catches it earlier so the reviewer finds out while the
context is still in their head.

A "good" verdict needs no correction: it is precisely the statement that the
model's own output was the right answer.

### Promotion is batched, and creates one version

Dataset versions are immutable (ADR 0004's reasoning, applied to datasets in
Phase 3). Promoting one item at a time would create a version per label — fifty
labels, fifty versions, and no way to say which run tested which set. One
promotion is one version.

The new version carries **every existing item plus the promoted ones**, because a
version is a complete snapshot rather than a delta. Each promoted example records
its provenance in metadata — source, review item id, trace id, verdict, reason,
who labelled it — so "where did this example come from?" is answerable months
later.

## Consequences

- **The eval set grows from production reality**, not from what someone imagined
  on day one. That is the flywheel, and it is now a code path rather than a
  paragraph in a README.
- **The heuristics have a measurable false-negative rate**, which almost no
  guardrail system reports about itself.
- **Promotion is one-way.** An item that has been promoted cannot be relabelled —
  its example is already frozen into a dataset version, and letting the label
  drift from the example would break the provenance the metadata claims.
- **False positives are expected and cheap.** `skip` clears one without recording
  a judgement, so a dismissed false positive does not pollute the label data used
  to assess the checks.
- **Single-reviewer labels.** There is no inter-annotator agreement measure: two
  people cannot label the same item, because a review item is unique per trace.
  For a team where labels are contested, the upgrade is a separate `review_labels`
  table keyed on `(item_id, reviewer)` — a contained change, since promotion is
  the only reader of the verdict.
- **Sampling reads spans per trace.** One query per sampled trace to build the
  snapshot, bounded by `MAX_TRACES_PER_RUN`. At a higher sampling rate the fix is
  a single windowed query joined across the batch, not a bigger limit.
