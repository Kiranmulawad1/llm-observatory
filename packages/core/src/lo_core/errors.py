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


class TemplateSyntaxError(ValidationError):
    """The Jinja2 source does not parse."""


class TemplateRenderError(ValidationError):
    """The template parses but failed to render, typically a missing variable."""
