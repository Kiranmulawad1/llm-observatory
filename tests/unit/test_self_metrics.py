"""The platform's own metrics.

Two things are worth testing here and they are not the obvious one. Whether a
counter increments is nearly tautological; whether the *cardinality* rule holds,
and whether the Grafana dashboard still refers to metrics that exist, are the
things that break quietly months later.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from lo_core import metrics

DASHBOARD = pathlib.Path(__file__).resolve().parents[2] / "infra/grafana/platform-health.json"

# Labels that would grow without bound. A Prometheus series exists forever once
# created — including for tenants that churned a year ago, because nothing tells
# Prometheus a project was deleted.
FORBIDDEN_LABELS = frozenset(
    {
        "project",
        "project_id",
        "project_slug",
        "model",
        "prompt",
        "prompt_version",
        "user",
        "trace_id",
    }
)


def _declared_metrics() -> dict[str, list[str]]:
    """Every lo_* metric family in the registry, with its label names."""
    found: dict[str, list[str]] = {}
    for collector in list(metrics.REGISTRY._collector_to_names):
        for metric in collector.collect():
            if metric.name.startswith("lo_"):
                found[metric.name] = list(getattr(collector, "_labelnames", ()))
    return found


class TestCardinality:
    """The rule the whole module exists to enforce."""

    def test_no_metric_is_labelled_per_tenant(self) -> None:
        offenders = {
            name: labels
            for name, labels in _declared_metrics().items()
            if FORBIDDEN_LABELS & set(labels)
        }
        assert not offenders, (
            f"unbounded label(s) on {offenders}. Per-tenant questions belong in "
            "TimescaleDB, which already answers them at /projects/{slug}/metrics."
        )

    def test_provider_latency_is_not_labelled_by_model(self) -> None:
        """Model names come from user-authored prompt versions.

        Labelling by them would let any tenant mint unbounded series in the
        platform's own monitoring simply by naming a model.
        """
        assert "model" not in metrics.provider_duration._labelnames
        assert "provider" in metrics.provider_duration._labelnames


class TestRegistry:
    def test_render_produces_prometheus_text_format(self) -> None:
        metrics.spans_ingested.labels(source="native").inc()
        body = metrics.render().decode()
        assert "# TYPE lo_spans_ingested_total counter" in body
        assert 'lo_spans_ingested_total{source="native"}' in body

    def test_process_collectors_are_present(self) -> None:
        """The default registry is used on purpose: 'how much memory is this
        pod using' should be answerable without extra wiring."""
        body = metrics.render().decode()
        assert "python_info" in body or "process_resident_memory_bytes" in body

    def test_histogram_buckets_cover_realistic_eval_runs(self) -> None:
        """The default buckets top out at 10s, which would put essentially
        every real eval run in +Inf and make the histogram useless.

        A labelled metric emits nothing until it has been observed, so this
        records one before reading the exposition — which is also the only way
        to test what a scrape genuinely returns rather than what was declared.

        Asserted on which buckets *exist*, not on their counts: the registry is
        a process-global singleton, so any other test that runs an eval run has
        already incremented this histogram. Coupling to those counts would make
        this pass alone and fail in the suite.
        """
        metrics.eval_run_duration.labels(status="succeeded").observe(240.0)
        body = metrics.render().decode()
        for bound in ("30.0", "300.0", "1800.0", "3600.0"):
            assert f'lo_eval_run_duration_seconds_bucket{{le="{bound}",status="succeeded"}}' in body


class TestGrafanaDashboard:
    """A dashboard that references a renamed metric fails silently.

    It renders, the panel is simply empty, and nobody notices until the outage
    it was supposed to show. Checking it against the registry turns that into a
    test failure at the moment of the rename.
    """

    def _dashboard(self) -> dict:
        return json.loads(DASHBOARD.read_text())

    def test_dashboard_is_valid_json_with_panels(self) -> None:
        d = self._dashboard()
        assert d["uid"] == "llm-observatory-platform"
        assert [p for p in d["panels"] if p["type"] != "row"]

    def test_every_referenced_metric_exists(self) -> None:
        declared = set(_declared_metrics())
        # Histograms expose _bucket/_sum/_count; counters expose _total. Strip
        # the suffix back to the family name the registry reports.
        suffixes = ("_bucket", "_sum", "_count", "_total")

        referenced: set[str] = set()
        for panel in self._dashboard()["panels"]:
            for t in panel.get("targets", []):
                referenced.update(re.findall(r"\blo_[a-z_]+\b", t["expr"]))

        missing = set()
        for name in referenced:
            candidates = {name}
            for suffix in suffixes:
                if name.endswith(suffix):
                    candidates.add(name[: -len(suffix)])
                    candidates.add(name[: -len(suffix)] + "_total")
            if not candidates & declared:
                missing.add(name)

        assert not missing, f"dashboard references metrics that do not exist: {sorted(missing)}"

    def test_dashboard_uses_a_datasource_variable(self) -> None:
        """Hardcoding a datasource uid makes the dashboard unimportable."""
        d = self._dashboard()
        assert d["__inputs"][0]["name"] == "DS_PROMETHEUS"
        for panel in d["panels"]:
            for t in panel.get("targets", []):
                assert t["datasource"]["uid"] == "${DS_PROMETHEUS}"

    @pytest.mark.parametrize("required", ["lo_queue_depth", "lo_provider_errors_total"])
    def test_operationally_important_metrics_are_charted(self, required: str) -> None:
        """Queue depth drives worker autoscaling; provider errors are the first
        thing to look at when eval runs start failing. A dashboard without them
        is missing the two panels someone opens it for."""
        body = json.dumps(self._dashboard())
        assert required in body
