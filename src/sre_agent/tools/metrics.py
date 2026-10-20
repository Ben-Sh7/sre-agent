"""query_metrics: a PromQL query against Prometheus.

Metrics hold numbers, not text that outsiders wrote, so a query may span
namespaces. Its cost is bounded instead: range, step and series count.
"""

from collections.abc import Mapping
from datetime import timedelta
from typing import Annotated

from pydantic import Field, StringConstraints

from sre_agent.sources import Series
from sre_agent.tools.base import Arguments, Tool, ToolContext, format_time, limited

MAX_SERIES = 200
POINTS_SHOWN = 12


class QueryMetricsArguments(Arguments):
    promql: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    range_minutes: Annotated[int, Field(ge=1, le=360)] | None = None
    step_seconds: Annotated[int, Field(ge=30, le=3600)] = 60


def _labels(labels: Mapping[str, str]) -> str:
    name = labels.get("__name__", "")
    pairs = ", ".join(
        f'{key}="{value}"' for key, value in sorted(labels.items()) if key != "__name__"
    )
    return f"{name}{{{pairs}}}"


def _number(value: float) -> str:
    return f"{value:.6g}"


def _summary(series: Series) -> str:
    if not series.points:
        return f"{_labels(series.labels)} no points"
    values = [value for _, value in series.points]
    every = max(1, len(series.points) // POINTS_SHOWN)
    shown = ", ".join(
        f"{format_time(time)[11:16]} {_number(value)}"
        for time, value in series.points[::every][-POINTS_SHOWN:]
    )
    return (
        f"{_labels(series.labels)} min {_number(min(values))} "
        f"max {_number(max(values))} last {_number(values[-1])} | {shown}"
    )


def query_metrics(arguments: QueryMetricsArguments, context: ToolContext) -> str:
    now = context.clock()
    if arguments.range_minutes is None:
        samples = sorted(
            context.metrics.instant(arguments.promql, now),
            key=lambda sample: _labels(sample.labels),
        )
        lines = [
            f"{_labels(sample.labels)} {_number(sample.value)}" for sample in samples
        ]
    else:
        series = sorted(
            context.metrics.range(
                arguments.promql,
                now - timedelta(minutes=arguments.range_minutes),
                now,
                timedelta(seconds=arguments.step_seconds),
            ),
            key=lambda one: _labels(one.labels),
        )
        lines = [_summary(one) for one in series]
    return "\n".join(limited(lines, MAX_SERIES, "series")) or "no data"


QUERY_METRICS = Tool(
    name="query_metrics",
    description=(
        "Run a PromQL query against Prometheus. Without range_minutes, returns the "
        "current value of each series. With range_minutes (up to 360), returns "
        "min, max, last and a few points per series, sampled every step_seconds "
        "(at least 30). At most 200 series are shown."
    ),
    arguments=QueryMetricsArguments,
    run=query_metrics,
)
