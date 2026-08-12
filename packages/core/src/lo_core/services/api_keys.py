"""API key issuance and verification.

The plaintext key exists for exactly one moment: the response to the create call.
After that only its hash is stored, so neither a database dump nor this codebase
can recover a working credential.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.config import get_settings
from lo_core.db.models.api_key import KEY_PREFIX_LENGTH, KEY_PREFIX_LIVE, ApiKey
from lo_core.errors import NotFoundError

# How stale `last_used_at` is allowed to get. Writing it on every request would
# add a row UPDATE to the ingest hot path — and turn a read-mostly table into a
# write-heavy one — for a timestamp whose only consumer is a human wondering
# whether a key is still in use. A minute of staleness is invisible to that.
LAST_USED_REFRESH = timedelta(minutes=1)


def generate_key() -> tuple[str, str]:
    """Mint a key. Returns `(plaintext, prefix)`.

    `token_urlsafe(32)` is 256 bits from the OS CSPRNG. That entropy is the
    entire security argument for the fast hash in db/models/api_key.py: guessing
    is not a threat model at this size.
    """
    secret = secrets.token_urlsafe(32)
    plaintext = f"{KEY_PREFIX_LIVE}{secret}"
    return plaintext, plaintext[:KEY_PREFIX_LENGTH]


def hash_key(plaintext: str) -> str:
    """SHA-256 over the key plus a server-side pepper.

    The pepper lives in configuration, never in the database. That is what makes
    a stolen database insufficient on its own: without the pepper an attacker
    cannot verify a guess offline, which is the property a per-row salt would
    otherwise provide.

    Rotating `LO_API_KEY_PEPPER` invalidates every issued key at once. That is
    the intended break-glass behaviour, not an accident.
    """
    pepper = get_settings().api_key_pepper.get_secret_value()
    return hashlib.sha256(f"{plaintext}{pepper}".encode()).hexdigest()


async def create_api_key(
    session: AsyncSession,
    project_id: uuid.UUID,
    name: str,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    description: str | None = None,
) -> tuple[ApiKey, str]:
    """Issue a key. Returns the record and the plaintext — show it once."""
    plaintext, prefix = generate_key()

    key = ApiKey(
        project_id=project_id,
        name=name,
        key_prefix=prefix,
        key_hash=hash_key(plaintext),
        scopes=scopes or ["ingest"],
        expires_at=expires_at,
        description=description,
    )
    session.add(key)
    await session.flush()
    return key, plaintext


async def verify_api_key(session: AsyncSession, plaintext: str) -> ApiKey | None:
    """Resolve a presented key, or None if it is invalid, revoked or expired.

    Looked up by its clear prefix so verification is one indexed read plus one
    comparison. Hashing every row in the table to find a match would make
    authentication cost grow with the number of issued keys — on the hottest
    endpoint in the system.
    """
    if not plaintext.startswith(KEY_PREFIX_LIVE):
        return None

    prefix = plaintext[:KEY_PREFIX_LENGTH]
    result = await session.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
    candidates = list(result.scalars().all())
    if not candidates:
        return None

    expected = hash_key(plaintext)
    now = datetime.now(UTC)

    for key in candidates:
        # `compare_digest`, not `==`. String equality short-circuits on the first
        # differing byte, so its timing leaks how much of a guess was correct —
        # enough to reconstruct a secret one byte at a time given enough attempts.
        if not hmac.compare_digest(key.key_hash, expected):
            continue
        if key.revoked_at is not None:
            return None
        if key.expires_at is not None and key.expires_at <= now:
            return None
        return key

    return None


async def touch_last_used(session: AsyncSession, key: ApiKey) -> None:
    """Record use, at most once per LAST_USED_REFRESH.

    Deliberately not awaited on the request's critical path by the caller — see
    the auth dependency. The read-before-write is what keeps this off the hot
    path in the common case.
    """
    now = datetime.now(UTC)
    if key.last_used_at is not None and now - key.last_used_at < LAST_USED_REFRESH:
        return
    await session.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now))


async def list_api_keys(session: AsyncSession, project_id: uuid.UUID) -> list[ApiKey]:
    result = await session.execute(
        select(ApiKey).where(ApiKey.project_id == project_id).order_by(ApiKey.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_api_key(session: AsyncSession, project_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey:
    """Revoke by timestamp rather than deleting.

    A deleted key leaves traces that reference a credential nobody can account
    for. A revoked one stays in the audit trail with the moment it stopped
    working.
    """
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.project_id == project_id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise NotFoundError(f"api key {key_id} not found")

    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await session.flush()
    return key
