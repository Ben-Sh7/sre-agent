"""The read-only tools the model may call, and the Toolbox that calls them."""

from typing import Any

from sre_agent.tools.base import Tool, Toolbox, ToolContext, ToolError, ToolResult
from sre_agent.tools.changes import GET_RECENT_CHANGES
from sre_agent.tools.kubernetes import (
    GET_ARGOCD_APP,
    GET_EVENTS,
    GET_POD_LOGS,
    GET_RESOURCE,
    LIST_RESOURCES,
)
from sre_agent.tools.logs import QUERY_LOGS
from sre_agent.tools.metrics import QUERY_METRICS

READ_ONLY_TOOLS: tuple[Tool[Any], ...] = (
    QUERY_METRICS,
    QUERY_LOGS,
    GET_RESOURCE,
    LIST_RESOURCES,
    GET_EVENTS,
    GET_POD_LOGS,
    GET_ARGOCD_APP,
    GET_RECENT_CHANGES,
)

__all__ = [
    "READ_ONLY_TOOLS",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "Toolbox",
]
