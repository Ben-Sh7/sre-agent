"""The report: the one way an investigation ends, and the checks it must pass.

The model finishes by calling submit_report. Before anyone sees the result,
code checks every piece of evidence against the tool output it cites, word for
word, and every field of the proposed action against the catalog, the policy,
the alert and the cluster. Whatever fails is removed and named - never kept
quietly, and never repaired by guessing.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from sre_agent.policy import Namespace, Policy
from sre_agent.sources import Kubernetes
from sre_agent.tools.base import Arguments, ObjectName, ToolResult

SUBMIT_REPORT = "submit_report"

Paragraph = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
Line = Annotated[str, StringConstraints(min_length=1, max_length=300)]


class Evidence(Arguments):
    call: Annotated[int, Field(ge=1)]
    quote: Line


class ProposedAction(Arguments):
    action: Literal["restart_workload", "delete_pod"]
    kind: Literal["Deployment", "StatefulSet", "Pod"]
    namespace: Namespace
    name: ObjectName

    @model_validator(mode="after")
    def _kind_fits_action(self) -> Self:
        allowed = {
            "restart_workload": ("Deployment", "StatefulSet"),
            "delete_pod": ("Pod",),
        }
        if self.kind not in allowed[self.action]:
            raise ValueError(
                f"{self.action} acts on {' or '.join(allowed[self.action])}"
            )
        return self


class Report(Arguments):
    """What the model submits. Field order is the order a person reads it in."""

    diagnosis: Paragraph
    determined: bool
    confidence: Literal["low", "medium", "high"]
    evidence: Annotated[list[Evidence], Field(max_length=10)]
    proposed_action: ProposedAction | None = None
    not_checked: Annotated[list[Line], Field(max_length=10)] = Field(
        default_factory=list
    )
    suggested_now: Annotated[list[Line], Field(max_length=5)] = Field(
        default_factory=list
    )
    suggested_permanent: Annotated[list[Line], Field(max_length=5)] = Field(
        default_factory=list
    )


SUBMIT_REPORT_TOOL: dict[str, Any] = {
    "name": SUBMIT_REPORT,
    "description": (
        "Finish the investigation. Call it exactly once, after the read-only tools. "
        "diagnosis: what is wrong, in plain words. determined: false when the "
        "evidence does not show a cause. evidence: the tool call number (call on "
        "the data tag) and an exact quote from its result. proposed_action: at "
        "most one action from the catalog, or none. not_checked: what you could "
        "not verify. suggested_now / suggested_permanent: steps for a person."
    ),
    "input_schema": Report.model_json_schema(),
}


@dataclass(frozen=True)
class ToolCall:
    """One call the model made, and exactly what came back."""

    number: int
    name: str
    arguments: Mapping[str, Any]
    result: ToolResult


@dataclass(frozen=True)
class CheckedEvidence:
    call: int
    tool: str
    quote: str


@dataclass(frozen=True)
class CheckedReport:
    report: Report
    evidence: tuple[CheckedEvidence, ...]
    dropped_evidence: tuple[str, ...]
    action: ProposedAction | None
    action_refused: str | None


def _words(text: str) -> str:
    return " ".join(text.split())


def check_evidence(
    report: Report, calls: Mapping[int, ToolCall]
) -> tuple[tuple[CheckedEvidence, ...], tuple[str, ...]]:
    """Keep the evidence whose quote really appears in the call it cites."""
    kept: list[CheckedEvidence] = []
    dropped: list[str] = []
    for item in report.evidence:
        call = calls.get(item.call)
        if call is None:
            dropped.append(f"call {item.call} never happened: {item.quote!r}")
        elif call.result.is_error:
            dropped.append(
                f"call {item.call} failed, so it proves nothing: {item.quote!r}"
            )
        elif _words(item.quote) not in _words(call.result.text):
            dropped.append(f"not in the result of call {item.call}: {item.quote!r}")
        else:
            kept.append(CheckedEvidence(item.call, call.name, item.quote))
    return tuple(kept), tuple(dropped)


def refuse_action(
    action: ProposedAction,
    policy: Policy,
    alert_labels: Mapping[str, str],
    kubernetes: Kubernetes,
) -> str | None:
    """Why this action may not be proposed, or None when it may."""
    allowed = policy.actions
    if policy.limits.max_actions_per_incident < 1:
        return "the policy allows no actions"
    if action.action == "restart_workload":
        if allowed.restart_workload is None:
            return "restart_workload is not allowed by the policy"
        if action.kind not in allowed.restart_workload.kinds:
            return f"restart_workload is not allowed on {action.kind}"
    if action.action == "delete_pod" and allowed.delete_pod is None:
        return "delete_pod is not allowed by the policy"
    if action.namespace not in policy.scope.act_in_namespaces:
        return f"the policy does not allow actions in {action.namespace}"
    alert_namespace = alert_labels.get("namespace")
    if alert_namespace is not None and alert_namespace != action.namespace:
        return f"the alert is about {alert_namespace}, not {action.namespace}"

    target = kubernetes.get_object(action.kind, action.namespace, action.name)
    if target is None:
        return f"{action.kind} {action.namespace}/{action.name} does not exist"
    if action.action == "delete_pod":
        owners = target["metadata"].get("ownerReferences", [])
        if not any(owner.get("controller") for owner in owners):
            return "the pod has no controller, so nothing would replace it"
    return None


def check_report(
    report: Report,
    calls: Mapping[int, ToolCall],
    policy: Policy,
    alert_labels: Mapping[str, str],
    kubernetes: Kubernetes,
) -> CheckedReport:
    evidence, dropped = check_evidence(report, calls)
    action = report.proposed_action
    refused = (
        refuse_action(action, policy, alert_labels, kubernetes) if action else None
    )
    return CheckedReport(
        report=report,
        evidence=evidence,
        dropped_evidence=dropped,
        action=action if refused is None else None,
        action_refused=refused,
    )
