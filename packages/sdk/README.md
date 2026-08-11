# llm-observatory-sdk

Client SDK for [llm-observatory](https://github.com/kiranmulawad/llm-observatory) — a
self-hosted evaluation and observability platform for LLM applications.

Instrumenting an existing app should be a three-line change:

```python
from llm_observatory import init, trace

init(api_key="lo_sk_...", endpoint="https://observatory.internal")

@trace(name="answer_question")
async def answer(question: str) -> str:
    ...
```

Nested spans (retrieval → rerank → generation) are linked automatically through
`contextvars`, so a call tree is reconstructed server-side without passing a
context object down every function signature.

## Design constraints

This package is installed into *other teams'* production applications, so it
holds itself to rules the platform services do not need:

- **One runtime dependency** (`httpx`). An observability SDK that drags in a
  dependency tree is an SDK teams refuse to adopt.
- **Never blocks the caller.** Spans go onto a bounded in-memory queue and are
  flushed by a background task. When the queue is full, spans are dropped and
  counted — the host application is never back-pressured by telemetry.
- **Never raises into the caller.** Every network and serialisation error is
  swallowed and recorded internally. Monitoring that can take down the service
  it monitors is worse than no monitoring.
- **No-ops when unconfigured.** An uninitialised SDK is inert, so importing it
  in a test suite or a CI run costs nothing.
- **Python 3.10+**, deliberately wider than the platform's own 3.13 floor: this
  package must not force a runtime upgrade on the teams adopting it.

## Status

Scaffolded in Phase 1; implementation lands in Phase 5 alongside the trace
ingestion API and the nested-span data model.
