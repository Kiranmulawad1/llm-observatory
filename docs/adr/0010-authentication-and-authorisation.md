# 0010 — Authentication and authorisation

Status: accepted
Date: 2026-08-19
Phase: 8

## Context

Phase 5 introduced project API keys, but only one endpoint checked them:
`POST /v1/traces`. Everything else — prompts, datasets, eval runs, judges,
alerts, the review queue — was open to anyone who could reach the port. That
was survivable while the platform was a single-tenant toy. It is not
survivable now that the data model is genuinely multi-tenant: every table
carries a `project_id`, and until this phase nothing enforced that a caller
was entitled to the project they named in the URL.

Two distinct callers need to authenticate, and they are not the same kind of
actor:

- **An instrumented application**, running on someone else's infrastructure,
  holding a project API key. It writes telemetry and nothing else.
- **The platform operator** — whoever runs this deployment, creates projects,
  and issues those keys in the first place. There is no project key that can
  mint project keys, because that would be a bootstrapping circle.

## Decision

### A single `Principal`, resolved once

Authentication produces one frozen dataclass:

```python
@dataclass(frozen=True)
class Principal:
    is_admin: bool = False
    key: ApiKey | None = None
    scopes: frozenset[str] = frozenset()
```

Both credential types arrive in the same `Authorization: Bearer` header and
are disambiguated by comparing against the operator token first
(`hmac.compare_digest`), then falling back to the API-key lookup. Routes never
see a raw header, and there is exactly one place in the codebase that turns
bytes into an identity.

### Tenancy is enforced in a dependency, not in handlers

`resolve_project` is the choke point. Every project-scoped route depends on
`CurrentProject`, which:

1. looks the project up by slug,
2. rejects the request if a project key's `project_id` does not match, and
3. requires `read` for safe methods and `write` for everything else.

This is the important structural decision. The alternative — an ownership
check inside each handler — is the design that produces IDOR bugs, because
it fails open: forget the check in one new handler and that handler is
public. Here a handler that wants the project *must* go through the
dependency to obtain it, so the check cannot be skipped by omission. A
handler is insecure only by actively asking for something else.

`tests/integration/test_auth.py` closes the loop by walking the live OpenAPI
schema and asserting every `{project_slug}` route 401s without a credential.
A new router added in a later phase is covered the moment it is registered,
with no test to remember to write.

### Cross-tenant access returns 404, not 403

Asking for a project you do not hold a key for is indistinguishable from
asking for a project that does not exist. A 403 would confirm the slug is
real, which is a free enumeration oracle for tenant names. Missing scope on
a project you *do* own still returns 403 — you already know it exists.

### Scopes: `ingest`, `read`, `write`, `admin`

`admin` implies `read` and `write` (a project administrator who cannot read
its own project is nonsense). `ingest` is deliberately implied by nothing,
including `admin`. Ingestion is the one capability exposed to code running
outside our infrastructure; keeping it un-implied means a leaked read-only
dashboard credential — or the operator token itself — cannot forge telemetry
into a project. The operator token can create keys and read anything, but
`POST /v1/traces` refuses it, because it carries no project of its own and
ingestion must be attributable to a specific issued key.

### The operator token is configuration, not a database row

`LO_ADMIN_TOKEN` is a settings value, and `assert_production_safe()` refuses
to boot a non-local environment without one of at least 32 characters. It is
compared in constant time. `make bootstrap` generates one with
`secrets.token_urlsafe(32)` so local development is authenticated too — there
is no dev-only bypass, because a dev-only bypass is a flag that eventually
ships enabled.

## Consequences

- Every endpoint now requires a credential; there is no anonymous surface
  except `/healthz` and `/readyz`, which must stay open for probes.
- The Next.js BFF holds `LO_ADMIN_TOKEN` server-side. The browser never sees
  a credential — this is why the proxy route exists and why it is GET-only.
- Tests run authenticated by default (the `client` fixture sends the operator
  token), so an auth-related test must opt out explicitly rather than
  accidentally passing because auth was off.

## What production at scale would add

- **Human identity.** A shared operator token has no notion of *who* acted.
  Real deployments front this with OIDC (Google Workspace, Okta) and issue
  short-lived per-user tokens, so the audit log names a person.
- **Key rotation with overlap.** Today revocation is immediate. Production
  wants two live keys per project during a rotation window.
- **Roles beyond four scopes.** Once "can approve a review-queue item" and
  "can edit alert rules" need to differ, this becomes RBAC with a role table.
- **An audit log.** Who issued which key, when, and what it touched — a
  `control.audit_events` table written on every mutating request.
- **Hashing cost.** SHA-256 + pepper is correct for a 256-bit random key on a
  hot ingest path, but the moment any human-chosen secret enters the system it
  must be Argon2id instead.
