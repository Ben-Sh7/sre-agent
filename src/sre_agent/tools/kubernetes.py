"""Read-only tools over the Kubernetes API.

Kinds come from an allowlist that never includes Secrets or ConfigMaps.
Namespaced objects are read only in the namespaces the policy names.
"""

import copy
from datetime import timedelta
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field

from sre_agent.policy import Namespace
from sre_agent.redaction import REDACTED
from sre_agent.sources import KubeObject
from sre_agent.tools.base import (
    Arguments,
    Label,
    ObjectName,
    Tool,
    ToolContext,
    ToolError,
    format_time,
    limited,
    parse_time,
)

# Where ArgoCD keeps its Application objects.
ARGOCD_NAMESPACE = "argocd"

Kind = Literal[
    "Pod",
    "Deployment",
    "StatefulSet",
    "ReplicaSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "Service",
    "EndpointSlice",
    "Ingress",
    "NetworkPolicy",
    "PersistentVolumeClaim",
    "PersistentVolume",
    "Node",
    "Namespace",
    "HorizontalPodAutoscaler",
    "PodDisruptionBudget",
    "Application",
]
CLUSTER_SCOPED = frozenset({"PersistentVolume", "Node", "Namespace"})

MAX_ITEMS = 100
MAX_EVENTS = 50
HISTORY_SHOWN = 5

_CONTAINER_LISTS = ("containers", "initContainers", "ephemeralContainers")
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _scope(context: ToolContext, kind: str, namespace: str | None) -> str | None:
    if kind in CLUSTER_SCOPED:
        if namespace is not None:
            raise ToolError(f"{kind} is not namespaced: leave namespace out")
        return None
    if namespace is None:
        raise ToolError(f"{kind} is namespaced: give a namespace")
    context.require_namespace(namespace)
    return namespace


def _hide_env_values(node: Any) -> None:
    """Replace the literal value of every container environment variable.

    Names and references to Secrets stay visible; a value written straight
    into a manifest may be a password, so it never reaches the model.
    """
    if isinstance(node, dict):
        for key, child in node.items():
            if key in _CONTAINER_LISTS and isinstance(child, list):
                for container in child:
                    for variable in container.get("env") or []:
                        if "value" in variable:
                            variable["value"] = REDACTED
            _hide_env_values(child)
    elif isinstance(node, list):
        for child in node:
            _hide_env_values(child)


def _clean(obj: KubeObject) -> KubeObject:
    """The object without bookkeeping the model has no use for, nor env values."""
    clean = copy.deepcopy(obj)
    metadata = clean.get("metadata", {})
    metadata.pop("managedFields", None)
    metadata.get("annotations", {}).pop(_LAST_APPLIED, None)
    if clean.get("kind") == "Node":
        clean.get("status", {}).pop("images", None)
    _hide_env_values(clean)
    return clean


class GetResourceArguments(Arguments):
    kind: Kind
    name: ObjectName
    namespace: Namespace | None = None


def get_resource(arguments: GetResourceArguments, context: ToolContext) -> str:
    namespace = _scope(context, arguments.kind, arguments.namespace)
    obj = context.kubernetes.get_object(arguments.kind, namespace, arguments.name)
    if obj is None:
        where = f" in {namespace}" if namespace else ""
        raise ToolError(f"{arguments.kind} {arguments.name} not found{where}")
    return yaml.safe_dump(_clean(obj), sort_keys=False, width=120).rstrip()


def _summary(kind: str, obj: KubeObject) -> str:
    name = obj["metadata"]["name"]
    spec = obj.get("spec", {})
    status = obj.get("status", {})
    if kind == "Pod":
        containers = status.get("containerStatuses", [])
        ready = sum(1 for container in containers if container.get("ready"))
        restarts = sum(container.get("restartCount", 0) for container in containers)
        return (
            f"{name} {status.get('phase', '?')} ready {ready}/{len(containers)} "
            f"restarts {restarts} node {spec.get('nodeName', '-')}"
        )
    if kind in ("Deployment", "StatefulSet", "ReplicaSet"):
        return (
            f"{name} ready {status.get('readyReplicas', 0)}/{spec.get('replicas', 0)}"
        )
    if kind == "DaemonSet":
        return (
            f"{name} ready {status.get('numberReady', 0)}"
            f"/{status.get('desiredNumberScheduled', 0)}"
        )
    if kind == "Node":
        node_ready = next(
            (c["status"] for c in status.get("conditions", []) if c["type"] == "Ready"),
            "?",
        )
        zone = obj["metadata"].get("labels", {}).get("topology.kubernetes.io/zone", "?")
        return f"{name} ready {node_ready} zone {zone}"
    if kind == "PersistentVolumeClaim":
        return f"{name} {status.get('phase', '?')} volume {spec.get('volumeName', '-')}"
    if kind == "Application":
        return (
            f"{name} sync {status.get('sync', {}).get('status', '?')} "
            f"health {status.get('health', {}).get('status', '?')}"
        )
    phase = status.get("phase")
    return f"{name} {phase}" if phase else name


class ListResourcesArguments(Arguments):
    kind: Kind
    namespace: Namespace | None = None


def list_resources(arguments: ListResourcesArguments, context: ToolContext) -> str:
    namespace = _scope(context, arguments.kind, arguments.namespace)
    objects = sorted(
        context.kubernetes.list_objects(arguments.kind, namespace),
        key=lambda obj: str(obj["metadata"]["name"]),
    )
    lines = [_summary(arguments.kind, obj) for obj in objects]
    return "\n".join(limited(lines, MAX_ITEMS, "items")) or f"no {arguments.kind} found"


class GetEventsArguments(Arguments):
    namespace: Namespace
    kind: Kind
    name: ObjectName


def _event_time(event: KubeObject) -> str:
    return str(
        event.get("lastTimestamp")
        or event.get("eventTime")
        or event["metadata"].get("creationTimestamp", "")
    )


def get_events(arguments: GetEventsArguments, context: ToolContext) -> str:
    context.require_namespace(arguments.namespace)
    events = context.kubernetes.events_for(
        arguments.namespace, arguments.kind, arguments.name
    )
    events.sort(key=lambda event: parse_time(_event_time(event)), reverse=True)
    lines = []
    for event in events[:MAX_EVENTS]:
        count = event.get("count", 1)
        times = f" (x{count})" if count > 1 else ""
        lines.append(
            f"{format_time(parse_time(_event_time(event)))} {event.get('type', '?')} "
            f"{event.get('reason', '?')}: {event.get('message', '')}{times}"
        )
    return "\n".join(lines) or f"no events for {arguments.kind} {arguments.name}"


class GetPodLogsArguments(Arguments):
    namespace: Namespace
    pod: ObjectName
    container: Label | None = None
    previous: bool = False
    tail_lines: Annotated[int, Field(ge=1, le=200)] = 100
    minutes: Annotated[int, Field(ge=1, le=60)] = 60


def _only_container(context: ToolContext, namespace: str, pod: str) -> str:
    obj = context.kubernetes.get_object("Pod", namespace, pod)
    if obj is None:
        raise ToolError(f"Pod {pod} not found in {namespace}")
    names = [container["name"] for container in obj["spec"]["containers"]]
    if len(names) != 1:
        raise ToolError(f"Pod {pod} has containers {names}: name one")
    return str(names[0])


def get_pod_logs(arguments: GetPodLogsArguments, context: ToolContext) -> str:
    context.require_namespace(arguments.namespace)
    container = arguments.container or _only_container(
        context, arguments.namespace, arguments.pod
    )
    text = context.kubernetes.pod_logs(
        arguments.namespace,
        arguments.pod,
        container,
        previous=arguments.previous,
        tail_lines=arguments.tail_lines,
        since=timedelta(minutes=arguments.minutes),
    )
    return text or "no log lines"


class GetArgocdAppArguments(Arguments):
    name: ObjectName


def get_argocd_app(arguments: GetArgocdAppArguments, context: ToolContext) -> str:
    context.require_namespace(ARGOCD_NAMESPACE)
    app = context.kubernetes.get_object("Application", ARGOCD_NAMESPACE, arguments.name)
    if app is None:
        raise ToolError(f"ArgoCD application {arguments.name} not found")
    status = app.get("status", {})
    sync = status.get("sync", {})
    health = status.get("health", {})
    operation = status.get("operationState", {})
    last_operation = f"{operation.get('phase', '?')} {operation.get('message', '')}"
    lines = [
        f"sync: {sync.get('status', '?')} at revision {sync.get('revision', '?')}",
        f"health: {health.get('status', '?')} {health.get('message', '')}".rstrip(),
        f"last operation: {last_operation}".rstrip(),
    ]
    lines += [
        f"condition {condition.get('type', '?')}: {condition.get('message', '')}"
        for condition in status.get("conditions", [])
    ]
    for entry in reversed(status.get("history", [])[-HISTORY_SHOWN:]):
        lines.append(
            f"deployed {str(entry.get('revision', '?'))[:12]} "
            f"at {format_time(parse_time(entry['deployedAt']))}"
        )
    return "\n".join(lines)


GET_RESOURCE = Tool(
    name="get_resource",
    description=(
        "Read one Kubernetes object as YAML. Leave namespace out for Node, "
        "PersistentVolume and Namespace. Secrets and ConfigMaps cannot be read, "
        "and the literal values of environment variables are hidden."
    ),
    arguments=GetResourceArguments,
    run=get_resource,
)
LIST_RESOURCES = Tool(
    name="list_resources",
    description=(
        "List the objects of one kind, with a one-line status each: in one "
        "namespace, or cluster-wide for Node, PersistentVolume and Namespace. "
        "At most 100 items."
    ),
    arguments=ListResourcesArguments,
    run=list_resources,
)
GET_EVENTS = Tool(
    name="get_events",
    description="Kubernetes events for one object, newest first. At most 50.",
    arguments=GetEventsArguments,
    run=get_events,
)
GET_POD_LOGS = Tool(
    name="get_pod_logs",
    description=(
        "The logs of one container. previous=true reads the run before the last "
        "restart. At most the last 200 lines, from up to the last 60 minutes. "
        "container may be left out when the pod has only one."
    ),
    arguments=GetPodLogsArguments,
    run=get_pod_logs,
)
GET_ARGOCD_APP = Tool(
    name="get_argocd_app",
    description=(
        "Sync and health status of an ArgoCD Application, its last operation, "
        "and the revisions it deployed most recently."
    ),
    arguments=GetArgocdAppArguments,
    run=get_argocd_app,
)
