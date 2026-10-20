"""Recorded incidents, replayed in place of the live sources.

A recording is a directory of JSON files: captured from a cluster, then
redacted and reviewed before it is committed, or written by hand for a test.

  incident.json    when it was recorded
  metrics.json     PromQL queries and their answers
  logs.json        log lines, filtered here the way Loki would filter them
  kubernetes.json  objects, events and pod logs
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from sre_agent.sources import (
    KubeObject,
    LogLine,
    LogQuery,
    Sample,
    Series,
    SourceError,
)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Incident(_Record):
    recorded_at: datetime


class _SampleRecord(_Record):
    labels: dict[str, str]
    value: float


class _SeriesRecord(_Record):
    labels: dict[str, str]
    points: list[tuple[datetime, float]]


class _MetricsRecord(_Record):
    query: str
    instant: list[_SampleRecord] | None = None
    range: list[_SeriesRecord] | None = None


class _LogRecord(_Record):
    time: datetime
    labels: dict[str, str]
    line: str


class _PodLogRecord(_Record):
    namespace: str
    pod: str
    container: str
    previous: bool = False
    text: str


class _KubernetesRecord(_Record):
    objects: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    pod_logs: list[_PodLogRecord] = Field(default_factory=list)


def _same_query(promql: str) -> str:
    """Compare queries without caring about spacing."""
    return " ".join(promql.split())


class RecordedMetrics:
    def __init__(self, records: list[_MetricsRecord]) -> None:
        self._records = {_same_query(record.query): record for record in records}

    def instant(self, promql: str, at: datetime) -> list[Sample]:
        record = self._records.get(_same_query(promql))
        if record is None or record.instant is None:
            raise SourceError("this query was not recorded")
        return [Sample(sample.labels, sample.value) for sample in record.instant]

    def range(
        self, promql: str, start: datetime, end: datetime, step: timedelta
    ) -> list[Series]:
        record = self._records.get(_same_query(promql))
        if record is None or record.range is None:
            raise SourceError("this range query was not recorded")
        return [
            Series(
                series.labels,
                [
                    (time, value)
                    for time, value in series.points
                    if start <= time <= end
                ],
            )
            for series in record.range
        ]


class RecordedLogs:
    def __init__(self, records: list[_LogRecord]) -> None:
        self._records = sorted(records, key=lambda record: record.time, reverse=True)

    def query(self, query: LogQuery) -> list[LogLine]:
        matches = [
            LogLine(record.time, record.labels, record.line)
            for record in self._records
            if record.labels.get("namespace") == query.namespace
            and (query.pod is None or record.labels.get("pod") == query.pod)
            and (
                query.container is None
                or record.labels.get("container") == query.container
            )
            and (query.contains is None or query.contains in record.line)
            and query.start <= record.time <= query.end
        ]
        return matches[: query.limit]


class RecordedKubernetes:
    def __init__(self, record: _KubernetesRecord) -> None:
        self._record = record

    def get_object(
        self, kind: str, namespace: str | None, name: str
    ) -> KubeObject | None:
        return next(
            (
                obj
                for obj in self.list_objects(kind, namespace)
                if obj["metadata"]["name"] == name
            ),
            None,
        )

    def list_objects(self, kind: str, namespace: str | None) -> list[KubeObject]:
        return [
            obj
            for obj in self._record.objects
            if obj["kind"] == kind and obj["metadata"].get("namespace") == namespace
        ]

    def events_for(self, namespace: str, kind: str, name: str) -> list[KubeObject]:
        return [
            event
            for event in self._record.events
            if event["involvedObject"]["kind"] == kind
            and event["involvedObject"]["name"] == name
            and event["metadata"]["namespace"] == namespace
        ]

    def pod_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
        since: timedelta,
    ) -> str:
        # The recording keeps the text only, so `since` cannot be applied here.
        for record in self._record.pod_logs:
            if (record.namespace, record.pod, record.container, record.previous) == (
                namespace,
                pod,
                container,
                previous,
            ):
                return "\n".join(record.text.splitlines()[-tail_lines:])
        raise SourceError(f"no logs recorded for {namespace}/{pod}/{container}")


class Recording:
    """One incident, read from its directory."""

    def __init__(self, directory: Path) -> None:
        self.recorded_at = _Incident.model_validate(
            _read(directory / "incident.json", None)
        ).recorded_at
        self.metrics = RecordedMetrics(
            TypeAdapter(list[_MetricsRecord]).validate_python(
                _read(directory / "metrics.json", [])
            )
        )
        self.logs = RecordedLogs(
            TypeAdapter(list[_LogRecord]).validate_python(
                _read(directory / "logs.json", [])
            )
        )
        self.kubernetes = RecordedKubernetes(
            _KubernetesRecord.model_validate(_read(directory / "kubernetes.json", {}))
        )


def _read(path: Path, default: object) -> Any:
    if not path.exists():
        if default is None:
            raise FileNotFoundError(path)
        return default
    return json.loads(path.read_text(encoding="utf-8"))
