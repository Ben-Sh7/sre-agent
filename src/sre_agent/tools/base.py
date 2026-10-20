"""What every read-only tool shares, and the one way any of them is called.

The model chooses a tool and its arguments; this code decides whether the call
is allowed. Every result is redacted, then cut to MAX_RESULT_CHARS, and a cut
result says so instead of hiding it.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Strict, StringConstraints, ValidationError

from sre_agent.policy import Policy
from sre_agent.redaction import redact
from sre_agent.sources import Kubernetes, Logs, Metrics, SourceError

MAX_RESULT_CHARS = 8000

# Kubernetes names: a label (namespace, container) and a subdomain (most objects).
Label = Annotated[
    str,
    Strict(),
    StringConstraints(max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"),
]
ObjectName = Annotated[
    str,
    Strict(),
    StringConstraints(max_length=253, pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"),
]


class Arguments(BaseModel):
    """A tool's arguments. Unknown fields and loose types are errors."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ToolError(Exception):
    """A call that is refused or cannot be done. The message goes to the model."""


@dataclass(frozen=True)
class ToolContext:
    policy: Policy
    metrics: Metrics
    logs: Logs
    kubernetes: Kubernetes
    clock: Callable[[], datetime]

    def require_namespace(self, namespace: str) -> None:
        allowed = self.policy.scope.investigate_namespaces
        if namespace not in allowed:
            raise ToolError(
                f"namespace {namespace} is outside the investigation scope: "
                f"{sorted(allowed)}"
            )


@dataclass(frozen=True)
class Tool[A: Arguments]:
    name: str
    description: str
    arguments: type[A]
    run: Callable[[A, ToolContext], str]

    def definition(self) -> dict[str, Any]:
        """The tool as the model API expects it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.arguments.model_json_schema(),
        }


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool


class Toolbox:
    """The tools the model may call, with the context they read from."""

    def __init__(self, tools: Sequence[Tool[Any]], context: ToolContext) -> None:
        self._tools: Mapping[str, Tool[Any]] = {tool.name: tool for tool in tools}
        self._context = context

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self._tools.values()]

    def call(self, name: str, arguments: object) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return _error(f"there is no tool named {name!r}")
        try:
            parsed = tool.arguments.model_validate(arguments)
        except ValidationError as error:
            return _error(f"invalid arguments for {name}: {describe_errors(error)}")
        try:
            text = tool.run(parsed, self._context)
        except (ToolError, SourceError) as error:
            return _error(str(error))
        return ToolResult(cap(redact(text)), is_error=False)


def cap(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    note = (
        f"\n[cut at {MAX_RESULT_CHARS} of {len(text)} characters: "
        "ask a narrower question]"
    )
    return text[: MAX_RESULT_CHARS - len(note)] + note


def limited(lines: list[str], most: int, what: str) -> list[str]:
    """At most `most` lines, and a last line that says how many were left out."""
    if len(lines) <= most:
        return lines
    return [*lines[:most], f"... {len(lines) - most} more {what} not shown"]


def format_time(time: datetime) -> str:
    return time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _error(message: str) -> ToolResult:
    return ToolResult(cap(redact(message)), is_error=True)


def describe_errors(error: ValidationError) -> str:
    """Where each problem is and what it is. The rejected values are not repeated."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'arguments'}: {item['msg']}"
        for item in error.errors(include_url=False, include_input=False)
    )
