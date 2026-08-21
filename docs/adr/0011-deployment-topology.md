# 0011 — Deployment topology: containers, Kubernetes, and GCP

Status: accepted
Date: 2026-08-19
Phase: 9

## Context

Phases 1–8 produced an application that runs on a laptop under
docker-compose. This phase makes it deployable: manifests that a cluster
accepts, and infrastructure-as-code for the substrate underneath.

One constraint shapes everything below, and it is worth stating plainly:
**this project has a hard $0 budget.** No cloud account, no billing, no
`terraform apply`. That is not a limitation to work around quietly — it is a
requirement, and the honest response is to say exactly which parts are
executed and which are only type-checked.

## Decision

### Kustomize, not Helm

Helm's value is parameterising a chart so that *other people* can install it
into clusters you have never seen. That is why every vendor ships one. This
deploys one application, which I control, into clusters I control.

The cost of Helm is that manifests stop being YAML and become Go templates
that emit YAML. Indentation is computed by `nindent`, conditionals wrap block
scalars, and a mistake is invisible until the render fails — or worse, until
it renders into something valid but wrong. Kustomize's base is real YAML:
readable in a diff, checkable by `kubeconform`, and directly appliable.
Overlays are patches against that YAML rather than branches inside it.

I would switch the moment someone outside this repository has to install the
platform, or the moment there are more than about four environments — that is
where "a patch per environment" stops scaling and templating starts paying.

### The database is self-hosted, and that is a constraint, not a preference

Cloud SQL for PostgreSQL does not support the `timescaledb` extension.
Migration `67465157137f` calls `create_hypertable()` on `telemetry.spans` and
`telemetry.traces`; against Cloud SQL it fails outright. AlloyDB does not
support it either.

The real options were:

1. **Timescale Cloud** — managed, correct, and a third-party SaaS with a bill.
   The right answer for a funded team. Rejected here on the budget constraint,
   and because it puts the primary datastore outside the VPC.
2. **Rewrite telemetry onto native declarative partitioning** — Cloud SQL can
   do this. It costs the things the hypertable was chosen for in ADR 0003:
   chunk exclusion on time-bounded queries, `time_bucket()`, and native
   compression. It is a storage-layer rewrite to accommodate a hosting choice,
   which is the tail wagging the dog.
3. **Self-host TimescaleDB on Kubernetes** — a StatefulSet with a
   PersistentVolumeClaim. Preserves the single-DSN architecture with zero code
   change, and is byte-identical to what runs locally.

Option 3 wins, with eyes open about what it costs: backups, failover, major
version upgrades and PVC resizing all become ours. "Run your database on
Kubernetes" is rightly treated as an anti-pattern *when a managed option
exists*; here one does not for this workload.

Because both environments run it, the StatefulSet lives in `base/` and the
overlays only patch its disk and resources. Redis is the counter-example and is deliberately managed (Memorystore,
`STANDARD_HA`): nothing forces our hand, so managed wins. The principle is
**managed where managed works, self-hosted only where a hard technical
constraint forces it** — not a blanket preference in either direction.

### Autopilot over Standard GKE

Autopilot bills per pod request, refuses privileged workloads, and takes node
management away. Nothing in this platform needs host access, and the removal
of "somebody has to remember to patch the nodes" eliminates the most common way
a small team's cluster rots. Workload Identity and Shielded Nodes are on by
default, which is a lot of posture you do not have to argue for in review.

Standard would win for GPU workloads with specific drivers, or where
bin-packing many small pods onto large spot nodes dominates the cost model.

### Secrets: three mechanisms, one interface

`base/` references a Secret named `lo-secrets` and knows nothing else. How it
comes to exist differs per environment:

| | source | mechanism |
| --- | --- | --- |
| local | `.env` | pydantic-settings |
| kind | gitignored `secrets.env`, generated with `secrets.token_urlsafe` | Kustomize `secretGenerator` |
| GCP | Secret Manager | External Secrets Operator + Workload Identity |

Two things are load-bearing here. First, **Terraform creates secret
containers, never versions.** A `google_secret_manager_secret_version` with
`secret_data` writes the plaintext into Terraform state, and state is a JSON
file in a bucket that CI can read; `sensitive = true` only redacts the CLI
display. Values are written out of band with `gcloud secrets versions add`.
Second, **no pod holds a Google credential**: Workload Identity exchanges a
projected, short-lived token, so there is no JSON key file to leak. Only the
External Secrets Operator talks to GCP at all; application pods read a
Kubernetes Secret, which keeps an application compromise inside the cluster.

The local cluster generates *real random* values rather than shipping a
placeholder, because a committed dev secret is the one that eventually gets
reused somewhere that matters.

### Migrations are a Job, and ordering is the pipeline's problem

An initContainer runs once per pod, so `replicas: 2` means two Alembic
processes racing on the same revision. A Job runs once. But `kubectl apply -k`
does not order resources, so the deploy sequence — create Job, wait for
completion, then roll the Deployments — lives in `make kind-deploy` and in CI,
not in the manifests. Pretending Kustomize orders things is how you ship code
that queries a column the database does not have yet.

### Probes: three questions, not one

- **startup** gates the others, so a slow boot is not counted as a liveness
  failure and restart-looped forever.
- **readiness** hits `/readyz`, which checks Postgres and Redis, so a database
  blip removes the pod from the Service and puts it back when it heals.
- **liveness** hits `/healthz`, which touches no dependency.

Liveness must not check the database. If it did, a database outage would
restart every pod in the fleet simultaneously and convert a recoverable
incident into a crash loop that outlives its cause.

### Memory limits, no CPU limits

A memory limit is a correctness boundary — exceed it and the container is
OOM-killed, which is the desired outcome for a leak. A CPU limit is throttling:
the kernel pauses the process mid-request even when the node is idle,
producing p99 latency nobody can explain. Requests already guarantee the share
the pod is entitled to under contention.

### NetworkPolicy is default-deny

With no policy, every pod can reach every other pod in the cluster.
`default-deny` plus explicit allows is what turns "the web pod is compromised"
into "the web pod is compromised and can reach exactly the API". Note that the
web tier has no route to Postgres at all: the BFF is an architectural boundary
from Phase 6, and this is the network enforcing what the code already promises.
Worker egress excludes RFC1918 and `169.254.0.0/16` — the metadata server hands
node credentials to anything that asks.

**A connection needs two policies to permit it**, not one: egress on the source
pod *and* ingress on the destination. They are evaluated independently. This
project shipped the classic version of that mistake — egress rules from
api/worker/migrate to the database, and no ingress rule on the database — and
the symptom is maximally misleading: Postgres `Running` and healthy,
`pg_isready` passing inside the pod, and every client timing out. Manifest
schema validation cannot catch it, because every field is valid. Only a cluster
that actually enforces NetworkPolicy can, which is the reason `kind-e2e` exists
in CI rather than schema validation alone.

The same run then exposed the corollary: **external traffic does not come from
a pod.** NodePort connections are SNAT'd to the node address, and cloud load
balancers arrive from the provider's health-check ranges. A policy expressed
purely in `podSelector` terms therefore drops every request from outside while
every in-cluster path stays healthy. Base declares only the in-cluster rules;
each overlay supplies the `ipBlock` for its own environment.

## What is actually executed, and what is not

| | executed? | how |
| --- | --- | --- |
| Dockerfiles | yes | built locally and in CI |
| Kustomize render | yes | `kubectl kustomize`, in CI for all three overlays |
| Manifest schemas | yes | `kubeconform -strict` against real API schemas |
| Manifests in a cluster | yes | a real `kind` cluster, in CI and locally |
| Terraform syntax and types | yes | `init` + `validate` against real provider schemas |
| Terraform formatting | yes | `fmt -check` |
| **Terraform apply** | **no** | no account, no billing, by design |

`terraform validate` is not a rubber stamp: it downloads the actual provider
schemas and type-checks every resource argument, so a misspelled attribute or
a wrong type fails. What it cannot catch is anything only the GCP API knows —
quota, IAM propagation, whether a machine type exists in a zone, whether the
KMS binding is sufficient. Those are unverified, and I would rather say so than
imply a green check means "this deploys".

There is deliberately no `make tf-apply` target.

## What production at scale would add

- **Backups for the self-hosted database.** A `VolumeSnapshot` schedule at
  minimum; realistically `pgBackRest` or the Timescale operator, with a
  *restore* rehearsed on a schedule. An untested backup is a hypothesis.
- **Queue-depth autoscaling for workers.** The HPA covers the API on CPU;
  workers should scale on arq queue depth via KEDA. Scaling them on CPU would
  add pods that sit blocked on provider 429s, so it is left out rather than
  approximated badly.
- **A service mesh or at least mTLS** between tiers. NetworkPolicy controls
  who may connect, not who they are.
- **Multi-zone for the database.** One StatefulSet replica is a single point
  of failure; a zone outage is an outage.
- **Progressive delivery.** Argo Rollouts or Flagger for canaries with
  automatic rollback on error rate, instead of a rolling update that trusts
  readiness probes.
- **Policy enforcement.** Pod Security Standards are set at the namespace, but
  Gatekeeper/Kyverno would enforce repository-wide invariants (no `:latest`,
  resources always set) rather than relying on review.
- **Cost attribution.** Labels are applied; nothing yet turns a billing export
  into a per-tenant number.
