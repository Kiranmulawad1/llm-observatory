# 0016 — Continuous delivery and the approval gate

Status: accepted
Date: 2026-08-22
Phase: 10

## Context

CI proves a commit is sound. Nothing yet takes a sound commit and puts it
anywhere. The last phase of the original plan is the pipeline that does —
including the human approval step before infrastructure changes, which the
brief called out specifically as "a real production practice — implement it,
don't skip it".

The $0 constraint applies unchanged: there is no GCP account, so this pipeline
never runs past its gate. What is worth building is the *shape* — what is
gated, what is immutable, what is reviewed — because that is not something you
retrofit onto a pipeline built to deploy straight from main.

## Decision

`build → push by digest → plan → human approval → apply → migrate → roll out`.

### The gate is a GitHub Environment

A job declaring `environment: production` blocks until a required reviewer
approves. That is the native mechanism, it records who approved which
deployment, and it cannot be bypassed by re-running one job.

The alternative is a third-party "wait for approval" action, which simulates
the same thing by opening an issue and polling it. Those exist because
Environments with required reviewers are a **paid** feature on private
repositories — a constraint this public repository does not have. Choosing the
workaround anyway would be cargo-culting.

### The plan artifact is what gets applied

This is the decision that makes the gate mean anything.

`terraform plan -out=tfplan` writes the exact set of changes to a file. That
file is uploaded, rendered into the job summary for a human to read, and after
approval the apply job downloads it and runs `terraform apply tfplan`.

The alternative — approving, then running a bare `terraform apply` — re-plans
against whatever the state looks like at approval time. If anything changed in
between, and something usually has, the reviewer approved one thing and the
pipeline applied another. Terraform refuses to apply a saved plan whose state
has moved on, so the property is enforced rather than hoped for: **what was
reviewed is what runs, or nothing runs.**

The plan file records every attribute of every resource, including ones marked
sensitive — it is a state document, not a diff. Hence five-day artifact
retention and no external upload.

`-detailed-exitcode` separates "no changes" (0) from "changes" (2) from "error"
(1). Without it a failed plan and an empty plan share an exit code, and the
pipeline would cheerfully present an error for approval.

### Deploy by digest, never by tag

The build job resolves each pushed tag to its digest and passes
`registry/image@sha256:…` onward; `kustomize edit set image` pins the manifests
to those digests.

A tag can be re-pointed at different bytes after the review that approved it. A
digest cannot. Artifact Registry is also configured with `immutable_tags`
(ADR 0011), so this is belt and braces — but the digest is the belt, because it
is what the manifests actually reference.

### Rollout is a separate job from apply

Infrastructure changes rarely; images change every merge. They fail for
unrelated reasons and at unrelated rates, and combining them would make a
failed rollout look like a failed infrastructure change in the run history.

Migrations run as a Job and the pipeline **waits** for it before rolling the
Deployments, because `kubectl apply -k` does not order resources (ADR 0011).

### Rollback is asymmetric, and the workflow says so

A failed rollout runs `kubectl rollout undo`. A failed *apply* does not get
reverted, and the failure message says why: Terraform has no undo, and reverting
infrastructure means generating and reviewing a new plan in the other direction.
Pretending otherwise — a "rollback" job that runs `terraform apply` against the
previous commit — would apply an unreviewed plan during an incident, which is
the worst possible moment to skip the gate.

### A preflight job, so an unconfigured repository stays green

Every job is gated on two repository *variables* being set. Without that, every
push to main would fail on credentials that are absent deliberately, and a red
X on main teaches people to ignore red Xs.

Skipped jobs render grey with a notice explaining what to set. It also means a
fork with a real GCP project enables the whole pipeline by setting variables,
with no code change.

## What is actually executed

| | executed? | how |
| --- | --- | --- |
| Workflow syntax, expressions, shell | yes | `actionlint` in CI, on every PR |
| Image build | yes | the same Dockerfiles CI builds |
| Digest pinning of manifests | yes | verified locally: all three images resolve to `@sha256:` and the overlay still passes `kubeconform` |
| Migrate-then-rollout ordering | yes | the same sequence `kind-e2e` runs on a real cluster |
| `terraform plan` / `apply` | **no** | no account, no billing, by design |
| The approval gate firing | **no** | nothing reaches it |

`actionlint` is the substitute for a dry run, and a real one: it type-checks
every `${{ }}` expression and shellchecks every `run:` block, which is where
workflow bugs actually live. It now runs in CI so the CD pipeline cannot rot
unnoticed while never being executed.

## Consequences

- The `production` environment and its required reviewers are repository
  settings, not code. They are documented in the README; a fork that skips them
  gets a pipeline that applies without asking, which is worth stating loudly.
- `GCP_DEPLOYER_SERVICE_ACCOUNT` and `GCP_WORKLOAD_IDENTITY_PROVIDER` come from
  Terraform outputs, so the infrastructure that CI authenticates against is
  described by the same configuration CI applies.
- No service account key exists anywhere. Authentication is Workload Identity
  Federation: GitHub mints an OIDC token, GCP exchanges it for a short-lived
  credential, and the provider is scoped to this repository by
  `attribute_condition` (ADR 0011) — without which any repository on GitHub
  could federate into the project.

## What production at scale would add

- **Progressive delivery.** This rolls all replicas after one smoke test.
  Argo Rollouts or Flagger would shift traffic gradually and roll back on error
  rate automatically, which is what the Phase 6 metrics exist to support.
- **A staging environment** that the same pipeline deploys to without approval,
  so the approved production plan has already been applied somewhere once.
- **Drift detection.** A scheduled `terraform plan` that alerts when reality
  and state diverge, rather than discovering it during a deploy.
- **Deployment markers** on the dashboards, so a latency change can be lined up
  against the deploy that caused it.
- **Image signing and provenance** (cosign, SLSA), so the digest being immutable
  is backed by a signature rather than by trusting the registry.
