"""Prompt template compilation, analysis and rendering.

Pure functions — no database, no I/O — so this module is cheap to test
exhaustively and is reused unchanged by the API, the worker and (eventually) the
eval runner.

Two decisions here carry real weight:

**SandboxedEnvironment, not the plain Environment.** Prompt templates are
authored through the API by whoever holds a project key, and stored templates are
rendered server-side. A plain Jinja2 environment makes that a server-side
template injection hole: `{{ ''.__class__.__mro__[1].__subclasses__() }}` is the
classic path from "can edit a template" to "can reach arbitrary Python objects".
The sandbox blocks access to underscore attributes and unsafe callables, which
turns prompt authoring back into a data operation rather than a code-execution
one.

**StrictUndefined, not the default Undefined.** By default Jinja2 renders an
unknown variable as the empty string. For prompts that failure mode is silent and
expensive: a typo'd `{{ contxt }}` produces a confident, well-formed prompt with
the retrieved context simply missing, and the resulting quality drop looks like a
model regression rather than a template bug. Strict undefined turns that into a
loud error at render time instead.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Any

from jinja2 import StrictUndefined, UndefinedError
from jinja2 import TemplateSyntaxError as JinjaSyntaxError
from jinja2.meta import find_undeclared_variables
from jinja2.sandbox import SandboxedEnvironment

from lo_core.errors import TemplateRenderError, TemplateSyntaxError
from lo_core.schemas.prompt import (
    Message,
    RenderedMessage,
    TemplateVariable,
)

# Prompts are text destined for a model API, not HTML for a browser. Autoescape
# would turn every apostrophe and angle bracket into an entity (`don&#39;t`),
# corrupting the text actually sent to the provider. The XSS concern autoescape
# addresses does not apply here; escaping for display is the frontend's job.
_ENV = SandboxedEnvironment(
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    # Templates come from the database as strings, never from the filesystem, so
    # there is no loader and no include/extends surface to secure.
    loader=None,
)

# A rendered prompt is bounded by what a model will accept, so an unbounded
# template expansion is always a bug. Note the limitation: the check runs after
# each message is rendered, so one message is still fully materialised before it
# is rejected. That caps the response and the damage, but it is not a memory
# guarantee — bounding the size of the *input* variables at ingestion is what
# would provide that, and belongs with request limits rather than here.
MAX_RENDERED_CHARS = 1_000_000


def compile_template(source: str) -> None:
    """Parse `source`, raising if it is not valid Jinja2.

    Called at write time so a broken template is rejected when it is registered,
    rather than at 3am when something tries to render it.
    """
    try:
        _ENV.parse(source)
    except JinjaSyntaxError as exc:
        raise TemplateSyntaxError(f"line {exc.lineno}: {exc.message}") from exc


def extract_variables(sources: Iterable[str]) -> list[TemplateVariable]:
    """Statically determine which variables a set of templates references.

    Uses Jinja2's own AST rather than a regex, so `{% for d in documents %}`
    correctly reports `documents` while *not* reporting the loop-local `d`.

    Ordering is alphabetical rather than source order: the result is persisted
    and compared across versions, so it needs to be deterministic regardless of
    which message a variable happened to appear in first.
    """
    names: set[str] = set()
    for source in sources:
        try:
            ast = _ENV.parse(source)
        except JinjaSyntaxError as exc:
            raise TemplateSyntaxError(f"line {exc.lineno}: {exc.message}") from exc
        names |= find_undeclared_variables(ast)

    return [TemplateVariable(name=name, required=True) for name in sorted(names)]


def render_messages(
    messages: Sequence[Message],
    variables: dict[str, Any],
) -> list[RenderedMessage]:
    """Render every message's template against `variables`.

    Raises `TemplateRenderError` on a missing variable rather than emitting a
    prompt with a hole in it.
    """
    rendered: list[RenderedMessage] = []
    total = 0

    for index, message in enumerate(messages):
        try:
            template = _ENV.from_string(message.content)
            text = template.render(**variables)
        except JinjaSyntaxError as exc:
            raise TemplateSyntaxError(f"message {index}, line {exc.lineno}: {exc.message}") from exc
        except UndefinedError as exc:
            raise TemplateRenderError(f"message {index}: {exc.message}") from exc
        except Exception as exc:  # sandbox violations and filter errors
            raise TemplateRenderError(f"message {index}: {exc}") from exc

        total += len(text)
        if total > MAX_RENDERED_CHARS:
            raise TemplateRenderError(
                f"rendered prompt exceeds {MAX_RENDERED_CHARS} characters; "
                "check for an unbounded loop in the template"
            )

        rendered.append(RenderedMessage(role=message.role, content=text))

    return rendered


def content_hash(messages: Sequence[Message], parameters: dict[str, Any]) -> str:
    """Stable SHA-256 over a version's semantic content.

    `sort_keys=True` is what makes this stable: without it, two dictionaries with
    identical contents but different insertion order would hash differently, and
    the "has this content already been registered?" check would produce a new
    version on every call.
    """
    payload = {
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "parameters": parameters,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
