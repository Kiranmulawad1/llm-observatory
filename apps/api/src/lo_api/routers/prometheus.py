"""The platform's own `/metrics`, for Prometheus.

Not to be confused with `routers/metrics.py`, which serves a *tenant's*
observability data at `/projects/{slug}/metrics`. The distinction is the whole
argument of `lo_core.metrics`: that endpoint answers "how is my application
behaving" for one project, this one answers "how is the platform behaving" for
whoever runs it.

### Why this endpoint is not authenticated

Every other endpoint requires a credential (ADR 0010). This one does not, for
the same reason `/healthz` does not: the scraper is infrastructure, it runs
inside the cluster, and giving Prometheus a platform-operator token so it can
poll every fifteen seconds would put that credential in a config file on a
schedule.

What makes it *safe* rather than merely convenient is the cardinality rule in
`lo_core.metrics`: no metric is labelled by project, model, prompt or user, so
there is no tenant data here to leak. The access control is the NetworkPolicy —
ingress only from the monitoring namespace — not a bearer token.

If that trade ever stops holding, the fix is bearer-token scraping
(`authorization` in the Prometheus scrape config), not adding tenant labels and
then trying to protect them.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from lo_core import metrics
from lo_core.logging import get_logger
from lo_core.queue import queue_depth

log = get_logger(__name__)

router = APIRouter(tags=["operations"])


@router.get(
    "/metrics",
    summary="Prometheus metrics for the platform itself",
    response_class=Response,
    include_in_schema=False,
)
async def prometheus_metrics() -> Response:
    """Render the registry.

    Queue depth is sampled here rather than in the worker, and that is
    deliberate: the queue is one shared Redis structure, so every worker replica
    reporting it would produce N identical series that a `sum()` would then
    silently multiply. The API is a single logical reader of a shared resource.
    """
    try:
        for name, depth in (await queue_depth()).items():
            metrics.queue_depth.labels(queue=name).set(depth)
    except Exception as exc:  # pragma: no cover - Redis down is its own alarm
        # A scrape must not fail because Redis is unreachable — the rest of the
        # registry is exactly what someone debugging that outage needs.
        log.warning("metrics.queue_depth_failed", error=str(exc))

    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)
