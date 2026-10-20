"""get_recent_changes: what changed in a namespace lately.

Many incidents start right after a change, so this is usually an early call.
It reads Kubernetes objects only: Deployment rollouts from their ReplicaSets,
and what ArgoCD deployed from its Applications. No GitHub token is needed.
"""

from datetime import datetime, timedelta
from typing import Annotated

from pydantic import Field

from sre_agent.policy import Namespace
from sre_agent.tools.base import Arguments, Tool, ToolContext, format_time, parse_time
from sre_agent.tools.kubernetes import ARGOCD_NAMESPACE

MAX_CHANGES = 20
_REVISION = "deployment.kubernetes.io/revision"


class GetRecentChangesArguments(Arguments):
    namespace: Namespace
    hours: Annotated[int, Field(ge=1, le=24)] = 24


def get_recent_changes(
    arguments: GetRecentChangesArguments, context: ToolContext
) -> str:
    context.require_namespace(arguments.namespace)
    since = context.clock() - timedelta(hours=arguments.hours)
    changes: list[tuple[datetime, str]] = []

    for replica_set in context.kubernetes.list_objects(
        "ReplicaSet", arguments.namespace
    ):
        metadata = replica_set["metadata"]
        created = parse_time(metadata["creationTimestamp"])
        owner = next(
            (
                ref["name"]
                for ref in metadata.get("ownerReferences", [])
                if ref["kind"] == "Deployment"
            ),
            None,
        )
        if created < since or owner is None:
            continue
        revision = metadata.get("annotations", {}).get(_REVISION, "?")
        images = ", ".join(
            container["image"]
            for container in replica_set["spec"]["template"]["spec"]["containers"]
        )
        changes.append(
            (created, f"Deployment {owner} rolled out revision {revision}: {images}")
        )

    notes = []
    if ARGOCD_NAMESPACE in context.policy.scope.investigate_namespaces:
        for app in context.kubernetes.list_objects("Application", ARGOCD_NAMESPACE):
            destination = app.get("spec", {}).get("destination", {})
            if destination.get("namespace") != arguments.namespace:
                continue
            for entry in app.get("status", {}).get("history", []):
                deployed = parse_time(entry["deployedAt"])
                if deployed >= since:
                    changes.append(
                        (
                            deployed,
                            f"ArgoCD application {app['metadata']['name']} deployed "
                            f"revision {str(entry.get('revision', '?'))[:12]}",
                        )
                    )
    else:
        notes.append(f"ArgoCD not checked: {ARGOCD_NAMESPACE} is outside the scope")

    changes.sort(reverse=True)
    lines = [f"{format_time(time)} {text}" for time, text in changes[:MAX_CHANGES]]
    if not lines:
        lines = [f"no changes in {arguments.namespace} in the last {arguments.hours} h"]
    return "\n".join([*lines, *notes])


GET_RECENT_CHANGES = Tool(
    name="get_recent_changes",
    description=(
        "What changed in a namespace in the last hours (up to 24), newest first: "
        "Deployment rollouts with their images, and the revisions ArgoCD deployed. "
        "Check it early - many incidents start right after a change."
    ),
    arguments=GetRecentChangesArguments,
    run=get_recent_changes,
)
