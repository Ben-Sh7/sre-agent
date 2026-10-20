"""query_logs: log lines from Loki, for one namespace at a time.

The model fills in fields instead of writing LogQL, so the namespace is always
one the policy lets it investigate. A live adapter builds the LogQL from them.
"""

from datetime import timedelta
from typing import Annotated

from pydantic import Field, StringConstraints

from sre_agent.policy import Namespace
from sre_agent.sources import LogQuery
from sre_agent.tools.base import (
    Arguments,
    Label,
    ObjectName,
    Tool,
    ToolContext,
    format_time,
)

Contains = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class QueryLogsArguments(Arguments):
    namespace: Namespace
    pod: ObjectName | None = None
    container: Label | None = None
    contains: Contains | None = None
    minutes: Annotated[int, Field(ge=1, le=60)] = 15
    limit: Annotated[int, Field(ge=1, le=200)] = 100


def query_logs(arguments: QueryLogsArguments, context: ToolContext) -> str:
    context.require_namespace(arguments.namespace)
    now = context.clock()
    lines = context.logs.query(
        LogQuery(
            namespace=arguments.namespace,
            pod=arguments.pod,
            container=arguments.container,
            contains=arguments.contains,
            start=now - timedelta(minutes=arguments.minutes),
            end=now,
            limit=arguments.limit,
        )
    )
    if not lines:
        return "no matching log lines"
    return "\n".join(
        f"{format_time(line.time)} {line.labels.get('pod', '-')} {line.line}"
        for line in lines
    )


QUERY_LOGS = Tool(
    name="query_logs",
    description=(
        "Read log lines from Loki for one namespace, newest first. Narrow it with "
        "pod, container, and contains (text the line must include). Covers up to "
        "the last 60 minutes, and at most 200 lines."
    ),
    arguments=QueryLogsArguments,
    run=query_logs,
)
