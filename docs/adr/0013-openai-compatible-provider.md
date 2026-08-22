# 0013 — One adapter for every OpenAI-compatible endpoint

Status: accepted
Date: 2026-08-22

## Context

`config.py` declared `openai_api_key` from Phase 1 and nothing ever read it:
`GENERATION_PROVIDERS` was `("fake", "anthropic")`. So the platform could only
evaluate against one vendor, which is an odd limitation for a tool whose entire
purpose is comparing prompts and models.

There is also a $0 constraint on this project. Any design that requires a paid
account to exercise a real model is a design that never gets exercised.

## Decision

**One provider module, with a configurable `base_url`.**

The OpenAI Chat Completions API is the de-facto interface for text generation.
Groq, Together, OpenRouter, Fireworks, vLLM and Ollama all speak it. So the
difference between them is a URL, not a code path:

```
LO_GENERATION_PROVIDER=openai
LO_OPENAI_BASE_URL=https://api.groq.com/openai/v1   # or Together, vLLM, Ollama…
```

The alternative — a module per vendor — means six near-identical adapters and
six places to fix the next time a response field moves. It also makes the
provider registry grow one entry per vendor forever, when the thing that varies
is configuration.

A direct consequence worth stating: pointing `LO_OPENAI_BASE_URL` at
`http://localhost:11434/v1` runs the whole eval engine against Ollama on a
laptop, with no account and no spend. That turns "real model evaluation" from
something this project cannot demonstrate into something it can.

### Cost is claimed only for OpenAI itself

`llama-3.3-70b` costs one thing on Groq, another on Together, and nothing per
token on a vLLM server you run yourself. A pricing table keyed on model name
cannot express that.

So the provider computes cost **only when `base_url` points at
`api.openai.com`**, and records `None` everywhere else. That follows the rule
already set in `pricing.py`: an unpriced model is *unknown* cost, not zero,
because a dashboard total that is silently wrong is worse than one that is
visibly incomplete. A confidently wrong cost figure is the number somebody
quotes in a meeting.

The endpoint's `base_url` is recorded in the span metadata, because months
later the stored row is the only thing that says which vendor served a run.

### Absent usage stays absent

Usage is optional in the OpenAI response schema and several compatible
gateways omit it. `None` propagates rather than becoming `0` — a run reporting
zero tokens looks free rather than unmeasured.

### The sampling-parameter check became provider-agnostic

ADR 0004 stores decoding parameters *with* the prompt version, and reasoning
models reject `temperature`/`top_p`/`top_k` with a 400. That was handled with an
Anthropic-specific branch in the evaluation service.

It is not an Anthropic problem — OpenAI's `o*` families behave identically. The
check now lives in `pricing.assert_sampling_supported` and runs for every
non-fake provider, so a third vendor does not mean a third `if`. OpenAI's model
names carry version suffixes (`o3-mini-2025-01-31`), so those families are
matched by prefix rather than exact membership.

### Structured outputs, and where they are not available

The judge depends on `response_schema` to make a parse failure impossible
rather than merely unlikely. OpenAI implements this as
`response_format: json_schema` with `strict: true`, which requires
`additionalProperties: false` and every property listed as required —
`JUDGE_RESPONSE_SCHEMA` already satisfies both.

Not every compatible gateway implements it. Rather than silently degrading to
unconstrained output — which would reintroduce exactly the silent judge rot the
schema exists to prevent — a rejection is caught and re-raised naming the
endpoint and the cause.

## What this exposed

`.env.example` ships the provider keys present but blank, so a fresh checkout
loads `SecretStr("")` rather than `None`. Every `is None` check therefore
believed a credential existed and passed `""` to the vendor SDK, which failed
with the SDK's own opaque error instead of the actionable one. This affected the
Anthropic provider too, and it was the *default* state of a new checkout — so it
would have hit essentially every first-time user.

Fixed at the boundary with a validator that normalises a blank secret to `None`,
which covers every consumer including ones added later.

## What production at scale would add

- **Per-endpoint pricing.** A `(base_url, model) → rate` table would let cost be
  computed for third-party gateways instead of abandoned.
- **A capability probe.** Structured-output support is discovered by being
  rejected. Asking `/models` at startup would surface it before a run.
- **Per-provider concurrency and rate limits.** Groq's limits differ sharply
  from OpenAI's; the runner currently uses one concurrency setting for all.
- **Streaming**, which the interface does not model at all today.
