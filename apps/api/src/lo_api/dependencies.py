"""Shared FastAPI dependencies, including the authentication choke point.

**Every project-scoped endpoint authenticates through `resolve_project`.** That
is not a convention anyone has to remember — it is structural. A handler needs a
`Project` to do anything, the only way to get one is this dependency, and this
dependency authenticates. Adding a new endpoint cannot accidentally skip auth,
because a handler with no project has nothing to operate on.

The alternative — a decorator or an explicit `Depends(require_auth)` on each
route — is one forgotten line away from an open endpoint, and that line is
forgotten in every codebase eventually.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Path, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.config import get_settings
from lo_core.db import get_sessionmaker
from lo_core.db.models.api_key import ApiKey
from lo_core.db.models.project import Project
from lo_core.errors import ForbiddenError, NotFoundError, UnauthorizedError
from lo_core.services import api_keys as api_key_service
from lo_core.services import projects as project_service

# Coarse capabilities. Deliberately few: a permission model nobody can hold in
# their head is one where everything ends up granted "just to make it work".
SCOPE_INGEST = "ingest"  # write spans
SCOPE_READ = "read"  # read anything within the project
SCOPE_WRITE = "write"  # create/modify prompts, datasets, runs, labels
SCOPE_ADMIN = "admin"  # manage the project's keys and guardrail settings

ALL_SCOPES = frozenset({SCOPE_INGEST, SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN})

# `admin` implies `read` and `write` **within its own project**. A project
# administrator that cannot read its own project is nonsense, and forcing
# ["read", "write", "admin"] everywhere is the kind of papercut that ends with
# somebody granting every scope to make an error go away.
#
# `ingest` is deliberately NOT implied. It is a machine-to-machine capability
# for a key embedded in someone else's application, and an operator wanting it
# should mint a key that says so.
IMPLIED_SCOPES: dict[str, frozenset[str]] = {
    SCOPE_ADMIN: frozenset({SCOPE_READ, SCOPE_WRITE}),
}

# Methods that only read. Everything else needs `write`.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def db_session() -> AsyncIterator[AsyncSession]:
    """One transaction per request, committed on success.

    Scoping the transaction to the request — rather than committing inside each
    service function — is what makes a multi-step handler atomic. Creating a
    prompt and its first version either both land or neither does, without the
    service layer needing to know it was called as part of a larger operation.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(db_session)]


@dataclass(frozen=True)
class Principal:
    """Whoever is making this request.

    Two kinds exist, and keeping them one type means every downstream check asks
    the same questions rather than branching on which credential was presented.
    """

    #: Platform operator (the admin token). Not scoped to any single project.
    is_admin: bool = False
    #: The project key, when one was presented.
    key: ApiKey | None = None
    scopes: frozenset[str] = field(default_factory=lambda: frozenset())

    def has(self, scope: str) -> bool:
        # The operator token is not a scope holder — it is above the scope
        # system entirely, because it is the thing that issues scoped keys.
        if self.is_admin or scope in self.scopes:
            return True
        return any(scope in IMPLIED_SCOPES.get(held, frozenset()) for held in self.scopes)


_bearer = HTTPBearer(auto_error=False, description="Project API key or platform admin token")


async def current_principal(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Resolve `Authorization: Bearer <token>` to a principal.

    Accepts either the platform admin token or a project API key. Health probes
    do not depend on this — a liveness check that needs a credential is a
    liveness check that fails during a credential outage, which is exactly when
    Kubernetes should not be restarting the fleet.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("missing Authorization header")

    presented = credentials.credentials
    admin_token = get_settings().admin_token

    # Constant-time comparison, same reasoning as the API key path: `==` leaks
    # through timing how much of a guess was correct.
    if admin_token is not None and hmac.compare_digest(presented, admin_token.get_secret_value()):
        return Principal(is_admin=True, scopes=frozenset(ALL_SCOPES))

    key = await api_key_service.verify_api_key(session, presented)
    if key is None:
        # One message for every failure mode — unknown, revoked, expired,
        # malformed. Distinguishing them tells an attacker which keys exist.
        raise UnauthorizedError("invalid or revoked credential")

    await api_key_service.touch_last_used(session, key)
    return Principal(is_admin=False, key=key, scopes=frozenset(key.scopes))


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def require_admin(principal: CurrentPrincipal) -> Principal:
    """Platform-operator only: creating projects, listing every project.

    A project key must never be able to create a *new* project — that would let
    a tenant mint themselves unlimited tenancy.
    """
    if not principal.is_admin:
        raise ForbiddenError("this endpoint requires the platform admin token")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]


async def resolve_project(
    request: Request,
    session: DbSession,
    principal: CurrentPrincipal,
    project_slug: Annotated[str, Path(description="Project slug")],
) -> Project:
    """Resolve `{project_slug}`, enforcing tenancy and scope.

    Three checks, in order:

    1. **The project exists.**
    2. **The principal may see it.** A project key belongs to exactly one
       project. Presenting it for another one returns **404, not 403** — a 403
       would confirm that the project exists, letting someone enumerate other
       tenants by slug.
    3. **The scope covers the method.** Read for safe methods, write otherwise,
       derived from the HTTP verb rather than declared per route. One rule in one
       place cannot drift out of sync with forty handlers, and a new endpoint
       inherits the correct requirement for free.
    """
    project = await project_service.get_project_by_slug(session, project_slug)

    if not principal.is_admin:
        key = principal.key
        if key is None or key.project_id != project.id:
            raise NotFoundError(f"project {project_slug!r} not found")

    required = SCOPE_READ if request.method in SAFE_METHODS else SCOPE_WRITE
    if not principal.has(required):
        raise ForbiddenError(f"this credential lacks the {required!r} scope")

    return project


CurrentProject = Annotated[Project, Depends(resolve_project)]


def require_scope(scope: str) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory for routes needing something beyond read/write.

    Used for the `ingest` scope on trace ingestion and `admin` on key management
    — cases the method-based default cannot express.
    """

    async def check(principal: CurrentPrincipal) -> Principal:
        if not principal.has(scope):
            raise ForbiddenError(f"this credential lacks the {scope!r} scope")
        return principal

    return check


#: Trace ingestion. The narrowest scope, and the only one the SDK needs — a key
#: embedded in a customer's application can write spans and nothing else.
IngestPrincipal = Annotated[Principal, Depends(require_scope(SCOPE_INGEST))]

#: Managing a project's own API keys and guardrail settings.
ProjectAdmin = Annotated[Principal, Depends(require_scope(SCOPE_ADMIN))]
