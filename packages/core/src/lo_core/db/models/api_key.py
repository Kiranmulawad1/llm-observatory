"""API keys.

Issued per project. This is the credential an external application presents when
it ships traces, so it is the one authentication surface that faces the internet
rather than a browser session.

**Only a hash is stored.** The plaintext key is returned exactly once, at
creation, and never again — a database dump must not hand an attacker working
credentials for every customer.

**Why SHA-256 and not bcrypt/argon2.** Password hashing is deliberately slow to
make guessing expensive, which is the right tradeoff for a human-chosen secret
with maybe 30 bits of entropy. An API key here is 256 bits of `secrets.token_*`
randomness — brute-forcing it is not on the table regardless of hash speed, so
slowness buys nothing. It costs a great deal: this hash is verified on *every*
ingest request, and a 100 ms KDF would cap trace throughput at ten spans per
second per core. A server-side pepper (`LO_API_KEY_PEPPER`) covers the case
bcrypt's salt would: a stolen database alone is not enough to verify guesses
offline.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lo_core.db.base import CONTROL_SCHEMA, ControlBase
from lo_core.db.mixins import CreatedAtMixin, UUIDPrimaryKey

# Keys look like `lo_live_<43 url-safe chars>`. The environment marker is in the
# key itself so a production key pasted into a staging config is visible to a
# human reading a diff, rather than being an opaque blob either way.
KEY_PREFIX_LIVE = "lo_live_"
# How much of the key is stored in clear for lookup and display. Long enough to
# be selective as an index, short enough to be useless to an attacker.
KEY_PREFIX_LENGTH = 16


class ApiKey(UUIDPrimaryKey, CreatedAtMixin, ControlBase):
    __tablename__ = "api_keys"
    __table_args__ = ({"schema": CONTROL_SCHEMA},)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # The clear leading segment, e.g. `lo_live_a1b2c3d4`. Indexed, so verifying a
    # presented key is one indexed lookup plus one hash comparison rather than a
    # scan that hashes every key in the table.
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Coarse capabilities. `ingest` is what the SDK needs and nothing more, so a
    # key embedded in a customer's application cannot read back other projects'
    # eval results or rotate its own credentials.
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default='["ingest"]')

    # Updated at most once a minute rather than on every request — see
    # services/api_keys.py. Writing on every ingest would add a row update to the
    # hot path purely for a timestamp nobody reads in real time.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Revocation is a timestamp, not a delete: an audit trail of which key was
    # active when survives, and a revoked key's traces stay attributable.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ApiKey {self.key_prefix}...>"
