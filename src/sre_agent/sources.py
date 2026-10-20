"""Where the read-only tools get their data.

The tools talk to these interfaces only. Phase 1 replays recorded incidents
(recorded.py); phase 2 adds live adapters for Prometheus, Loki and the
Kubernetes API. Every adapter is read-only, and a live one must give up on a
call after TOOL_TIMEOUT_SECONDS.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

TOOL_TIMEOUT_SECONDS = 10

# A Kubernetes object as the API returns it: plain JSON data.
KubeObject = dict[str, Any]


class SourceError(Exception):
    """The source could not answer: unreachable, too slow, or nothing recorded."""


@dataclass(frozen=True)
class Sample:
    labels: Mapping[str, str]
    value: float


@dataclass(frozen=True)
class Series:
    labels: Mapping[str, str]
    points: Sequence[tuple[datetime, float]]


@dataclass(frozen=True)
class LogQuery:
    namespace: str
    pod: str | None
    container: str | None
    contains: str | None
    start: datetime
    end: datetime
    limit: int


@dataclass(frozen=True)
class LogLine:
    time: datetime
    labels: Mapping[str, str]
    line: str


class Metrics(Protocol):
    def instant(self, promql: str, at: datetime) -> list[Sample]: ...

    def range(
        self, promql: str, start: datetime, end: datetime, step: timedelta
    ) -> list[Series]: ...


class Logs(Protocol):
    def query(self, query: LogQuery) -> list[LogLine]:
        """Lines matching the query, newest first."""
        ...


class Kubernetes(Protocol):
    def get_object(
        self, kind: str, namespace: str | None, name: str
    ) -> KubeObject | None: ...

    def list_objects(self, kind: str, namespace: str | None) -> list[KubeObject]: ...

    def events_for(self, namespace: str, kind: str, name: str) -> list[KubeObject]: ...

    def pod_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
        since: timedelta,
    ) -> str: ...
