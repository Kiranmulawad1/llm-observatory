# ADR 0006 — Judges as prompts, retrieval metrics, and run comparison

**Status:** Accepted (Phase 4)

## Context

Phase 3 could score outputs deterministically. Three things were missing:

1. Subjective questions — *is this answer faithful to the context?* — that no
   regex can express.
2. Any signal about the **retriever**, which for a RAG system is where most bad
   answers actually originate.
3. The question the platform exists to answer: **did this change make things
   worse?**

## Decision

### A judge rubric is a prompt, stored in the prompt registry

Not a separate `judge_rubrics` table, and not a constant in code. A `kind`
discriminator on `prompts` separates judges from application prompts.

A judge prompt needs exactly what an application prompt needs: immutable
versions, movable labels, diffs, server-side rendering. A parallel table would
mean a second version-numbering scheme, a second diff implementation and a second
label mechanism, all of which ADR 0004 already solved.

More importantly it makes the rubric **pinnable**. `eval_runs` records
`judge_prompt_version_id`, so a score change is attributable. Without that pin,
"faithfulness fell from 0.8 to 0.6" is ambiguous between a worse model and a
stricter rubric — and a rubric edit is by far the easier of the two to make by
accident.

Built-in rubrics (correctness, faithfulness, relevance, toxicity) are **seed
content**, written into a project's registry on request. Seeding is idempotent
*by slug*, never by content: a project that already has `judge-faithfulness` is
left completely alone, including its edits. Overwriting a team's rubric on
deploy would silently change the meaning of every score it subsequently produced.

### The judge returns schema-constrained output

`output_config.format` with a JSON schema, not prose parsed with a regex.

Regex-parsing a judge is how these evaluations rot silently: the model rephrases
("I'd say a solid 4" → "Rating: four"), the pattern stops matching, every example
scores null, and the run still reports success. Constrained generation makes the
parse failure impossible rather than merely unlikely.

This added `response_schema` to `GenerationRequest`, and the fake provider emits
conforming JSON — so judge behaviour is testable for free and deterministically.

### The scale is 1-5 with written anchors, normalised to 0.0-1.0

Integers, not a raw float: asked for a 0-1 score, models return 0.85 for almost
everything. A small labelled scale is where the gaps mean something.

Every built-in rubric describes all five points, and a test enforces that. An
unanchored "rate 1-5" produces a judge that clusters at 3-4 and discriminates
nothing — the rubric text *is* the measuring instrument.

The default pass threshold is 4/5, not the midpoint, because judged scores are
noisiest in the middle.

### Self-judging is recorded, not blocked

A model rates its own output more favourably. When the judge model equals the
model under test, the score detail says so. Blocking it would be wrong —
sometimes you genuinely want it — but a hidden caveat is worse than a visible
one.

### Retrieval metrics are deterministic and need no model

precision@k, recall@k and MRR compare retrieved documents against ground truth.
No model call, so they are the cheapest useful signal a RAG team can gate CI on:
run them on every commit and reserve the judge for cases where retrieval was fine
and the answer still looks wrong.

They answer a question answer-quality metrics cannot: *was the model ever given
the right passage?* A prompt change cannot fix a passage the retriever never
returned.

**Matching is normalised exact match, not fuzzy.** A similarity threshold would
make the metric depend on an embedding model, and "recall improved" would become
ambiguous between a better retriever and a different embedder — reintroducing
exactly the ambiguity this platform exists to remove.

MRR is kept alongside precision because it is rank-sensitive: models attend
unevenly across long context, so the right passage at position 1 is not
equivalent to the same passage at position 20.

### Comparison aligns examples by identity, and refuses when it cannot

Two runs over the same dataset version are matched by `dataset_item_id`.
Comparing across dataset versions requires an explicit `align=positional` and
attaches a warning.

Positional alignment looks equivalent and is not. Insert one row into a dataset
and every subsequent index shifts, so example *n* in one run is a different
question from example *n* in the other, and every reported regression is a
comparison between unrelated examples. That is precisely the confidently-wrong
answer this tool exists to catch, and shipping it as the default would be the
worst kind of irony.

**Everything that differed is surfaced as a warning** — prompt version, model,
judge rubric, dataset version — because a score delta means something completely
different depending on which of them moved.

## Consequences

- **One judge per run.** Scores are unique per `(result, evaluator)`, so two
  `llm_judge` evaluators collide on insert; the registry's duplicate-name check
  rejects it at creation. Comparing two rubrics means two runs, which is also the
  more honest experiment.
- **Judging is the expensive path.** Default judge model is the strongest tier,
  because a judge is the thing deciding whether a change ships and a judge weaker
  than the model it grades produces scores nobody trusts. It is per-run
  configurable for teams that would rather run it on every commit.
- **Rubric edits are invisible without the pin.** The pin exists, and comparison
  warns when two runs used different rubric versions — but nothing prevents
  someone editing a rubric and drawing the wrong conclusion from a single run in
  isolation. A guardrail here (refuse to promote a rubric without a re-baseline)
  is a natural follow-up.
- **Retrieval metrics score what they are given.** The retrieved set comes from a
  dataset field, so the platform is scoring a retriever run elsewhere. Phase 5's
  retrieval spans become the second source, at which point the same evaluators
  read from a trace.
- **Example-level "regressed" is conservative.** Mixed movement across evaluators
  is reported as `unchanged` with the per-evaluator detail intact, because
  collapsing a genuine tradeoff into one verdict is a judgement the tool has no
  basis to make.
