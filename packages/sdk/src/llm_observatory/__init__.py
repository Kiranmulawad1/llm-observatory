"""llm-observatory — tracing client for LLM applications.

Three lines to integrate:

    from anthropic import Anthropic
    from llm_observatory import configure, instrument, trace, span

    configure(api_key="lo_live_...", endpoint="https://your-platform")
    client = instrument(Anthropic())

    @trace("answer_question")
    def answer(question: str) -> str:
        with span("retrieval", kind="retrieval") as s:
            docs = retriever(question)
            s.set_output(docs)

        return client.messages.create(...)   # captured automatically

`configure()` reads `LO_API_KEY` and `LO_ENDPOINT` from the environment when not
passed explicitly, and can be skipped entirely — the library configures itself
lazily on first use.

**Without an API key, everything here is a no-op.** No thread, no network, no
error. Importing this into a project that has not set it up costs nothing.

This package depends only on `httpx` and supports Python 3.10+, which is wider
than the platform's own 3.13 floor — it is installed into *other people's*
applications, and it must not dictate their Python version or drag SQLAlchemy
into their dependency tree. See ADR 0001.
"""

from llm_observatory._client import TracingClient, configure, get_client
from llm_observatory._span import Span
from llm_observatory._tracing import current_trace_id, instrument, span, trace

__version__ = "0.1.0"

__all__ = [
    "Span",
    "TracingClient",
    "configure",
    "current_trace_id",
    "get_client",
    "instrument",
    "span",
    "trace",
]
