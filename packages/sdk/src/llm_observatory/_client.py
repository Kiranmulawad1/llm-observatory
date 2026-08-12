"""The tracing client: bounded buffer, background flush, never raises.

This module has one hard requirement above all others:

> **Instrumentation must never break, block, or slow the application it is
> instrumenting.**

An observability tool that takes down the service it observes is worse than no
observability tool. Every design choice here follows from that:

* **A bounded queue.** If the platform is unreachable, spans accumulate. An
  unbounded buffer would grow until the host process is OOM-killed — the tracing
  library would have caused an outage. The queue has a hard cap and drops the
  oldest spans when full, keeping a count so the loss is visible rather than
  silent.

* **A background thread, not asyncio.** The host application may be sync
  (Flask, a script) or async (FastAPI). A daemon thread with a plain queue works
  identically in both. An asyncio flusher would require a running event loop and
  would simply not work in half of them.

* **Every public call is wrapped.** A bug here, a network failure, a malformed
  payload — none of it escapes into the caller's stack. The worst case is losing
  telemetry, which is always better than failing the user's request.

* **Inert when unconfigured.** No API key means every operation is a cheap
  no-op. Importing this library into a project that has not set it up costs
  nothing and fails nowhere.
"""

from __future__ import annotations

import atexit
import queue
import threading
import time
from typing import Any

DEFAULT_ENDPOINT = "http://localhost:8000"
DEFAULT_QUEUE_SIZE = 10_000
DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL = 2.0
DEFAULT_TIMEOUT = 10.0
# Ingest is idempotent by (trace_id, span_id), so a retry that duplicates a
# successful-but-unacknowledged send is harmless. That is what makes retrying
# safe at all.
DEFAULT_MAX_RETRIES = 3


class TracingClient:
    """Buffers spans and ships them in the background."""

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        debug: bool = False,
    ) -> None:
        import os

        self.api_key = api_key or os.getenv("LO_API_KEY")
        self.endpoint = (endpoint or os.getenv("LO_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.debug = debug

        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_size)
        self._dropped = 0
        self._sent = 0
        self._failed = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._session: Any = None

        # No key means no destination. Stay completely inert rather than
        # spawning a thread that will only ever fail to connect.
        if self.enabled:
            self._start()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # --- lifecycle --------------------------------------------------------

    def _start(self) -> None:
        self._worker = threading.Thread(
            target=self._run,
            name="llm-observatory-flush",
            # Daemon: a hung flush must never stop the host process from
            # exiting. The atexit hook below gets a bounded chance to drain
            # first, and after that losing spans beats hanging on shutdown.
            daemon=True,
        )
        self._worker.start()
        atexit.register(self.shutdown)

    def _run(self) -> None:
        """Flush loop. Wakes on a full batch or on the interval, whichever first."""
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()

        while not self._stop.is_set():
            timeout = max(0.0, self.flush_interval - (time.monotonic() - last_flush))
            try:
                batch.append(self._queue.get(timeout=timeout))
            except queue.Empty:
                pass

            due = time.monotonic() - last_flush >= self.flush_interval
            if batch and (len(batch) >= self.batch_size or due):
                self._send(batch)
                batch = []
                last_flush = time.monotonic()

        # Drain whatever is left at shutdown.
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._send(batch)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the worker after giving it a bounded chance to drain.

        Bounded on purpose. An unbounded join would let an unreachable endpoint
        hang the host process on exit — turning a telemetry problem into a
        deployment problem.
        """
        if self._worker is None:
            return
        self._stop.set()
        self._worker.join(timeout=timeout)
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: S110 - shutdown must not raise
                pass

    # --- the hot path -----------------------------------------------------

    def submit(self, payload: dict[str, Any]) -> None:
        """Queue a span. Never blocks, never raises.

        `put_nowait` rather than `put`: blocking here would push backpressure
        from *our* platform into the *caller's* request handler, which is
        precisely the failure mode this whole design exists to prevent.
        """
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            # Drop the oldest and keep the newest — recent telemetry is more
            # useful during an incident than a backlog from ten minutes ago.
            with self._lock:
                self._dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass
        except Exception:  # noqa: S110 - see module docstring
            # Belt and braces: nothing from this library reaches the caller.
            pass

    def _send(self, batch: list[dict[str, Any]]) -> None:
        try:
            self._post(batch)
        except Exception as exc:
            with self._lock:
                self._failed += len(batch)
            if self.debug:
                print(f"[llm-observatory] flush failed: {exc}")  # noqa: T201

    def _post(self, batch: list[dict[str, Any]]) -> None:
        import httpx

        if self._session is None:
            # One client for the process. A fresh connection per flush would
            # pay a TCP and TLS handshake every couple of seconds.
            self._session = httpx.Client(timeout=self.timeout)

        url = f"{self.endpoint}/v1/traces"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        for attempt in range(self.max_retries):
            try:
                response = self._session.post(url, json={"spans": batch}, headers=headers)
            except Exception:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(2**attempt)
                continue

            if response.status_code < 300:
                with self._lock:
                    self._sent += len(batch)
                return

            if response.status_code == 429:
                # Honour the server's backoff instead of guessing. A client that
                # guesses under load is how a rate limit becomes a stampede.
                retry_after = int(response.headers.get("Retry-After", 2**attempt))
                if attempt == self.max_retries - 1:
                    raise RuntimeError("rate limited")
                time.sleep(min(retry_after, 30))
                continue

            if 400 <= response.status_code < 500:
                # A bad key or a malformed batch will fail identically forever.
                # Retrying wastes the budget and delays the spans behind it.
                raise RuntimeError(f"ingest rejected batch: {response.status_code}")

            if attempt == self.max_retries - 1:
                raise RuntimeError(f"ingest failed: {response.status_code}")
            time.sleep(2**attempt)

    # --- introspection ----------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Counters, so span loss is observable rather than silent."""
        with self._lock:
            return {
                "queued": self._queue.qsize(),
                "sent": self._sent,
                "dropped": self._dropped,
                "failed": self._failed,
            }

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the queue drains. For tests and short-lived scripts.

        Not for use on a request path — it is the one blocking call in the
        library, and it exists because a script that exits immediately would
        otherwise lose everything it recorded.
        """
        if not self.enabled:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty():
                time.sleep(self.flush_interval + 0.1)
                return True
            time.sleep(0.05)
        return False


_client: TracingClient | None = None
_client_lock = threading.Lock()


def configure(**kwargs: Any) -> TracingClient:
    """Set up the global client. Call once at application start."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.shutdown()
        _client = TracingClient(**kwargs)
    return _client


def get_client() -> TracingClient:
    """The global client, created from the environment on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = TracingClient()
    return _client
