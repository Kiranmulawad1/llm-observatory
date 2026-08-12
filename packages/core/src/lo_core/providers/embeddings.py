"""Local embedding provider.

Runs a small ONNX sentence-transformer inside the worker process. Chosen over a
hosted embedding API so that cloning this repository and running a full eval
requires no API key and costs nothing — which also means CI exercises the real
embedding code path rather than only the fake.

The costs are real and worth stating: roughly 150-250 MB of extra image size for
onnxruntime plus the model file, and a few hundred MB of resident memory in the
worker. That is why `fastembed` is an *optional* dependency of `lo-core`
(`lo-core[local-embeddings]`) that only `apps/worker` installs — the API image
never carries a model it does not load.

At production scale the tradeoff flips: a hosted embedding endpoint gives better
quality and moves the memory out of the worker's autoscaling profile. That is a
provider swap behind `EmbeddingProvider`, not a rewrite.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from lo_core.errors import ValidationError
from lo_core.providers.base import EmbeddingProvider

# Small, fast, widely used. 384 dimensions is plenty for answer-similarity
# scoring and keeps the model file around 100 MB.
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_LOCAL_DIMENSIONS = 384


class LocalEmbeddingProvider(EmbeddingProvider):
    """ONNX embeddings via `fastembed`."""

    name = "local"

    def __init__(self, model: str = DEFAULT_LOCAL_MODEL) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise ValidationError(
                "local embeddings require the optional dependency: "
                "install lo-core[local-embeddings]"
            ) from exc

        # Loading downloads the model on first use and takes seconds. Done once
        # per provider instance, which the runner creates once per eval run.
        self._model: Any = TextEmbedding(model_name=model)
        self._model_name = model
        self.dimensions = DEFAULT_LOCAL_DIMENSIONS

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch.

        `fastembed` is synchronous and CPU-bound, so it runs in a worker thread.
        Calling it directly would block the event loop for the whole batch and
        stall every other in-flight example in the run — the classic way an
        async service quietly serialises itself.
        """
        return await asyncio.to_thread(self._embed_sync, list(texts))

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, vector)) for vector in self._model.embed(texts)]
