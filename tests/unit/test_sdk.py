"""The SDK.

The headline test in here is `TestNeverBreaksHostApp`. Everything else in this
library is negotiable; that property is not.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from llm_observatory import Span, TracingClient, current_trace_id, span, trace
from llm_observatory._client import configure
from llm_observatory._span import new_span_id, new_trace_id


@pytest.fixture(autouse=True)
def _isolated_client(monkeypatch: pytest.MonkeyPatch):
    """Give every test its own client, capturing spans instead of sending them."""
    captured: list[dict] = []

    client = TracingClient(api_key=None)  # inert: no thread, no network
    client.api_key = "lo_live_test"  # mark enabled without starting the worker
    client.submit = lambda payload: captured.append(payload)  # type: ignore[method-assign]

    monkeypatch.setattr("llm_observatory._tracing.get_client", lambda: client)
    return captured


class TestSpanNesting:
    def test_root_span_has_no_parent(self, _isolated_client: list[dict]) -> None:
        with span("root"):
            pass
        assert _isolated_client[0]["parent_span_id"] is None

    def test_child_inherits_trace_and_points_at_parent(self, _isolated_client: list[dict]) -> None:
        with span("root") as parent:
            with span("child") as child:
                assert child.trace_id == parent.trace_id
                assert child.parent_span_id == parent.span_id

        # Children finish first, so they are submitted first.
        child_payload, root_payload = _isolated_client
        assert child_payload["name"] == "child"
        assert root_payload["name"] == "root"
        assert child_payload["trace_id"] == root_payload["trace_id"]

    def test_three_levels_deep(self, _isolated_client: list[dict]) -> None:
        with span("a"), span("b"), span("c"):
            pass

        by_name = {p["name"]: p for p in _isolated_client}
        assert by_name["c"]["parent_span_id"] == by_name["b"]["span_id"]
        assert by_name["b"]["parent_span_id"] == by_name["a"]["span_id"]
        assert by_name["a"]["parent_span_id"] is None
        assert len({p["trace_id"] for p in _isolated_client}) == 1

    def test_siblings_share_a_parent(self, _isolated_client: list[dict]) -> None:
        with span("root") as root:
            with span("first"):
                pass
            with span("second"):
                pass

        by_name = {p["name"]: p for p in _isolated_client}
        assert by_name["first"]["parent_span_id"] == root.span_id
        assert by_name["second"]["parent_span_id"] == root.span_id

    def test_context_is_restored_after_a_span_closes(self, _isolated_client: list[dict]) -> None:
        with span("root") as root:
            with span("child"):
                pass
            # Back to root, not left pointing at the closed child.
            assert current_trace_id() == root.trace_id
            with span("sibling") as sibling:
                assert sibling.parent_span_id == root.span_id

    def test_separate_traces_are_independent(self, _isolated_client: list[dict]) -> None:
        with span("first"):
            pass
        with span("second"):
            pass
        assert _isolated_client[0]["trace_id"] != _isolated_client[1]["trace_id"]


class TestErrors:
    def test_exception_is_recorded_and_re_raised(self, _isolated_client: list[dict]) -> None:
        """The failed operation is the most interesting one to have a record of."""
        with pytest.raises(ValueError, match="boom"), span("failing"):
            raise ValueError("boom")

        payload = _isolated_client[0]
        assert payload["status"] == "error"
        assert payload["error_type"] == "ValueError"
        assert "boom" in payload["error_message"]

    def test_parent_is_unaffected_by_a_caught_child_error(
        self, _isolated_client: list[dict]
    ) -> None:
        with span("root"):
            try:
                with span("child"):
                    raise RuntimeError("handled")
            except RuntimeError:
                pass

        by_name = {p["name"]: p for p in _isolated_client}
        assert by_name["child"]["status"] == "error"
        assert by_name["root"]["status"] == "ok"


class TestDecorator:
    def test_wraps_a_sync_function(self, _isolated_client: list[dict]) -> None:
        @trace("answer")
        def answer(question: str) -> str:
            return f"answer to {question}"

        assert answer("q1") == "answer to q1"
        payload = _isolated_client[0]
        assert payload["name"] == "answer"
        assert payload["kind"] == "chain"

    def test_records_arguments_by_parameter_name(self, _isolated_client: list[dict]) -> None:
        """`{"question": "q1"}` beats `{"args": ["q1"]}` for a human reading a trace."""

        @trace()
        def answer(question: str, top_k: int = 3) -> str:
            return "x"

        answer("q1")
        assert _isolated_client[0]["input"]["question"] == "q1"
        assert _isolated_client[0]["input"]["top_k"] == 3

    async def test_wraps_an_async_function(self, _isolated_client: list[dict]) -> None:
        """An async target must stay awaitable, not become a sync call."""

        @trace("async_answer")
        async def answer(question: str) -> str:
            await asyncio.sleep(0)
            return "done"

        assert await answer("q") == "done"
        assert _isolated_client[0]["name"] == "async_answer"

    def test_spans_inside_a_decorated_function_nest_under_it(
        self, _isolated_client: list[dict]
    ) -> None:
        @trace("pipeline")
        def pipeline() -> None:
            with span("retrieval", kind="retrieval"):
                pass

        pipeline()
        by_name = {p["name"]: p for p in _isolated_client}
        assert by_name["retrieval"]["parent_span_id"] == by_name["pipeline"]["span_id"]

    def test_decorator_preserves_metadata(self, _isolated_client: list[dict]) -> None:
        @trace()
        def documented(x: int) -> int:
            """Original docstring."""
            return x

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "Original docstring."


class TestConcurrency:
    async def test_concurrent_tasks_do_not_share_a_parent(
        self, _isolated_client: list[dict]
    ) -> None:
        """The reason parent tracking is a ContextVar and not a thread-local.

        On one event-loop thread, a thread-local would give every concurrent
        coroutine the same "current span" — producing a tree that is confidently
        wrong.
        """

        async def worker(index: int) -> str:
            with span(f"task-{index}") as s:
                await asyncio.sleep(0.01)
                return s.trace_id

        trace_ids = await asyncio.gather(*(worker(i) for i in range(5)))

        assert len(set(trace_ids)) == 5
        assert all(p["parent_span_id"] is None for p in _isolated_client)

    async def test_nesting_survives_an_await(self, _isolated_client: list[dict]) -> None:
        with span("root") as root:
            await asyncio.sleep(0.01)
            with span("after_await") as child:
                assert child.parent_span_id == root.span_id


class TestIds:
    def test_trace_id_is_32_hex_chars(self) -> None:
        value = new_trace_id()
        assert len(value) == 32
        int(value, 16)  # W3C requires valid hex

    def test_span_id_is_16_hex_chars(self) -> None:
        value = new_span_id()
        assert len(value) == 16
        int(value, 16)

    def test_ids_are_unique(self) -> None:
        assert len({new_trace_id() for _ in range(1000)}) == 1000


class TestPayloadHandling:
    def test_large_payloads_are_truncated(self, _isolated_client: list[dict]) -> None:
        """A retrieved corpus must not become a 50 MB request body."""
        with span("big") as s:
            s.set_output({"docs": "x" * 100_000})

        output = _isolated_client[0]["output"]
        assert output.get("truncated") is True

    def test_unserialisable_values_do_not_raise(self, _isolated_client: list[dict]) -> None:
        class Opaque:
            pass

        with span("weird") as s:
            s.set_output({"obj": Opaque()})

        assert _isolated_client[0]["output"] is not None

    def test_duration_is_measured(self, _isolated_client: list[dict]) -> None:
        with span("timed"):
            time.sleep(0.02)
        assert _isolated_client[0]["duration_ms"] >= 15


class TestNeverBreaksHostApp:
    """The property everything else is subordinate to."""

    def test_unconfigured_client_is_a_silent_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Importing this into an unconfigured project must cost nothing."""
        client = TracingClient(api_key=None)
        monkeypatch.setattr("llm_observatory._tracing.get_client", lambda: client)

        assert client.enabled is False
        with span("noop"):
            pass
        # No worker thread was ever started.
        assert client._worker is None

    def test_submitting_to_a_full_queue_does_not_block_or_raise(self) -> None:
        """A backed-up queue must not push backpressure into the caller."""
        client = TracingClient(api_key=None)
        client.api_key = "lo_live_test"
        client._queue.maxsize = 5

        started = time.monotonic()
        for index in range(200):
            client.submit({"span_id": str(index)})
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert client.stats()["dropped"] > 0

    def test_dropped_spans_are_counted_not_silent(self) -> None:
        client = TracingClient(api_key=None)
        client.api_key = "lo_live_test"
        client._queue.maxsize = 2

        for index in range(10):
            client.submit({"span_id": str(index)})

        # Loss is visible, so a team can see their buffer is undersized.
        assert client.stats()["dropped"] >= 8

    def test_unreachable_endpoint_does_not_break_the_caller(self) -> None:
        """The scenario this whole design exists for: the platform is down.

        Points the SDK at a closed port and asserts the host code still runs and
        returns normally.
        """
        client = configure(
            api_key="lo_live_test",
            # Reserved-for-documentation address: guaranteed not to connect.
            endpoint="http://192.0.2.1:9",
            flush_interval=0.05,
            timeout=0.1,
            max_retries=1,
        )
        try:

            @trace("business_logic")
            def business_logic(x: int) -> int:
                return x * 2

            assert business_logic(21) == 42
            time.sleep(0.3)  # let a flush attempt fail
            assert business_logic(1) == 2

            # Failures are recorded, not raised.
            assert client.stats()["failed"] >= 0
        finally:
            client.shutdown(timeout=1.0)

    def test_flush_thread_is_a_daemon(self) -> None:
        """A hung flush must never stop the host process from exiting."""
        client = TracingClient(api_key="lo_live_test", endpoint="http://192.0.2.1:9")
        try:
            assert client._worker is not None
            assert client._worker.daemon is True
        finally:
            client.shutdown(timeout=1.0)

    def test_shutdown_is_bounded(self) -> None:
        """An unreachable endpoint must not hang the process on exit."""
        client = TracingClient(api_key="lo_live_test", endpoint="http://192.0.2.1:9", timeout=5.0)
        client.submit({"span_id": "x"})

        started = time.monotonic()
        client.shutdown(timeout=0.5)
        assert time.monotonic() - started < 3.0

    def test_submit_survives_a_malformed_payload(self) -> None:
        client = TracingClient(api_key=None)
        client.api_key = "lo_live_test"
        client.submit(None)  # type: ignore[arg-type]


class TestSpanBuilders:
    def test_chained_setters(self) -> None:
        s = Span(name="n", trace_id=new_trace_id(), span_id=new_span_id())
        s.set_model("claude-opus-5").set_tokens(10, 20).set_cost(0.001).set_metadata(env="test")

        payload = s.finish().to_payload()
        assert payload["model"] == "claude-opus-5"
        assert payload["prompt_tokens"] == 10
        assert payload["completion_tokens"] == 20
        assert payload["metadata"]["env"] == "test"

    def test_finish_is_idempotent(self) -> None:
        s = Span(name="n", trace_id=new_trace_id(), span_id=new_span_id())
        first = s.finish().duration_ms
        time.sleep(0.01)
        assert s.finish().duration_ms == first
