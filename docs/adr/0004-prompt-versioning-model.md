# ADR 0004 — Prompt versioning: immutable versions, movable labels

**Status:** Accepted (Phase 2)

## Context

Everything else this platform does references a prompt: an eval run records which
prompt it scored, and a production trace records which prompt served the request.
Both are only worth storing if "prompt version 7" means the same thing in a year
as it does today.

That forces the data model to answer three questions:

1. What exactly was sent to the model on this request?
2. Which prompt is in production *right now*?
3. What changed between two versions, and did it matter?

## Decision

Three tables, mirroring the shape container registries and model registries
converged on independently:

| Table            | Role                                | Mutable? |
| ---------------- | ----------------------------------- | -------- |
| `prompts`        | stable identity (`support-triage`)   | metadata only |
| `prompt_versions`| immutable content snapshot           | never |
| `prompt_labels`  | movable pointer (`production` → v7)  | the pointer only |

### Versions are immutable and append-only

Editing a prompt appends a new row; nothing is ever updated. `prompt_versions`
therefore has `created_at` and no `updated_at` — the absence is the schema
stating the guarantee.

This is what makes question 1 answerable. If version 7's text could be edited in
place, every eval result and trace referencing it would silently start
misdescribing what ran, and the platform's core claim — that you can trust its
history — would be false.

### Labels are rows, not a column

`production` / `staging` / `experimental` are pointers stored as
`(prompt_id, label) → version_id`, with a unique constraint on the pair.

The alternative — a `label` column on `prompt_versions` — fails question 2 under
concurrency. Moving "production" from v6 to v7 would be two UPDATEs, and between
them a reader sees either two production versions or none. As a single upserted
row (`INSERT … ON CONFLICT DO UPDATE`), promotion is one atomic statement.

Labels are free-form rather than an enum. Teams invent their own (`canary`,
`eu-rollout`), and an enum would require a migration each time.

### Content is a chat message array

`[{role, content}]` matching provider APIs, rather than one flat string. A
single-string prompt is the one-element case, so this is a superset. It keeps the
system instruction versioned and diffable separately from the user turn — and in
practice the system message is where most regressions are introduced.

Model parameters (`temperature`, `model`, …) are stored *with* the text, because
a temperature change alters behaviour as much as a wording change does. A diff
that ignored them would report "no change" for a version that behaves
differently, which is the failure mode this platform exists to catch.

### Templates are sandboxed Jinja2

`SandboxedEnvironment` with `StrictUndefined`.

Sandboxed because templates are authored through the API and rendered
server-side; a plain `Environment` turns "can edit a prompt" into server-side
template injection via `{{ ''.__class__.__mro__ }}`. Strict-undefined because
Jinja2's default renders an unknown variable as empty string — so a typo'd
`{{ contxt }}` yields a well-formed prompt with the context silently missing, and
the quality drop looks like a model regression rather than a template bug.

Declared variables are extracted from the Jinja AST at write time (not by regex,
which would wrongly report loop locals) and persisted, so the API can validate
inputs and the UI can render a form without recompiling the template.

### Diffs are computed server-side and returned structured

One implementation shared by the web UI, the SDK and a future CI gate. Structured
rather than a formatted blob so a machine consumer can ask "did the system
message change, or only the temperature?" — the question that decides whether a
change needs a fresh eval run.

Messages are compared positionally rather than by similarity: position is
meaning, and a similarity matcher would report a swapped system/user pair as
"unchanged, reordered".

### Projects exist now, before authentication

`projects` is created in this phase even though API keys are Phase 8. Tenancy has
to be in the schema from the first table, because adding a `project_id` foreign
key to populated tables later means inventing a project for every existing row.

## Consequences

- **Version numbering needs serialising.** `max(version) + 1` is a read-then-write
  race. The prompt row is locked with `SELECT … FOR UPDATE` so concurrent writers
  to the same prompt queue up; writers to *different* prompts never contend. The
  unique constraint on `(prompt_id, version)` remains as the correctness backstop.
- **Storage grows without bound.** Every edit is a row, and nothing is ever
  deleted. Prompts are small and low-volume so this is fine for a long time; at
  real scale the answer is archiving old unlabelled versions to object storage,
  not deletion, since traces reference them.
- **Reverting duplicates content.** Rolling back to v3's text creates v9 with an
  identical `content_hash`. This is deliberate: the history should record that a
  revert happened. The hash is indexed so a CI job can detect "nothing actually
  changed" and skip creating a version.
- **`lazy="raise"` on all relationships.** Serialising a prompt cannot
  accidentally trigger a per-row query; the N+1 fails loudly instead of quietly
  working in development and collapsing in production.
