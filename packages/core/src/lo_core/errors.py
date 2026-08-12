"""Domain exceptions.

`core` raises these; the API layer maps them to HTTP status codes. The domain
deliberately knows nothing about HTTP — that is what lets the worker, and later
a CLI, reuse the same service functions without dragging FastAPI in.
"""

from __future__ import annotations


class LOError(Exception):
    """Base class, so a caller can catch everything this platform raises."""


class NotFoundError(LOError):
    """A referenced resource does not exist. Maps to 404."""


class ConflictError(LOError):
    """The request collides with existing state, e.g. a duplicate slug. Maps to 409."""


class ValidationError(LOError):
    """Semantically invalid input that Pydantic cannot express. Maps to 422."""


class UnauthorizedError(LOError):
    """No credential, or one that does not resolve. Maps to 401.

    401 and 403 are genuinely different and worth keeping apart: 401 means "I do
    not know who you are, try authenticating", 403 means "I know who you are and
    the answer is no". A client can act on the first by refreshing a key; the
    second is never worth retrying.
    """


class ForbiddenError(LOError):
    """A valid credential that lacks the required scope. Maps to 403."""


class RateLimitError(LOError):
    """The caller exceeded its quota. Maps to 429."""

    def __init__(self, message: str, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TemplateSyntaxError(ValidationError):
    """The Jinja2 source does not parse."""


class TemplateRenderError(ValidationError):
    """The template parses but failed to render, typically a missing variable."""
