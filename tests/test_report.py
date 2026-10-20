"""The checks a report passes before anyone sees it."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sre_agent.policy import Actions, Policy, RestartWorkload, load_policy
from sre_agent.recorded import Recording
from sre_agent.report import (
    ProposedAction,
    Report,
    ToolCall,
    check_evidence,
    refuse_action,
)
from sre_agent.tools import ToolResult

ROOT = Path(__file__).parents[1]
KUBERNETES = Recording(
    ROOT / "tests" / "fixtures" / "redis-stuck-connection"
).kubernetes
POLICY = load_policy(ROOT / "examples" / "policy.yaml", own_namespace="sre-agent")
ALERT_LABELS = {"alertname": "TaskManagerBackendErrors", "namespace": "task-manager"}
BAD_POD = "backend-deploy-7d9f8c6b5-x7k2p"

CALLS = {
    1: ToolCall(
        1,
        "query_logs",
        {"namespace": "task-manager"},
        ToolResult(f"2026-10-20T03:18:44Z {BAD_POD} Redis   command timed out", False),
    ),
    2: ToolCall(
        2, "query_metrics", {"promql": "up"}, ToolResult("not recorded", is_error=True)
    ),
}


def _report(**fields: Any) -> Report:
    return Report.model_validate(
        {"diagnosis": "d", "determined": True, "confidence": "high", **fields}
    )


def _action(**fields: str) -> ProposedAction:
    base = {"action": "delete_pod", "kind": "Pod", "namespace": "task-manager"}
    return ProposedAction.model_validate({**base, "name": BAD_POD, **fields})


# Evidence


def test_a_quote_from_the_result_is_kept_whatever_its_spacing() -> None:
    kept, dropped = check_evidence(
        _report(evidence=[{"call": 1, "quote": "Redis command timed out"}]), CALLS
    )

    assert [(item.call, item.tool) for item in kept] == [(1, "query_logs")]
    assert dropped == ()


@pytest.mark.parametrize(
    ("call", "quote", "reason"),
    [
        pytest.param(
            1, "connection refused", "not in the result of call 1", id="made up"
        ),
        pytest.param(9, "Redis command timed out", "never happened", id="no such call"),
        pytest.param(2, "not recorded", "proves nothing", id="a failed call"),
    ],
)
def test_evidence_that_does_not_hold_is_dropped(
    call: int, quote: str, reason: str
) -> None:
    kept, dropped = check_evidence(
        _report(evidence=[{"call": call, "quote": quote}]), CALLS
    )

    assert kept == ()
    assert reason in dropped[0]


# The proposed action


def test_an_allowed_action_passes() -> None:
    assert refuse_action(_action(), POLICY, ALERT_LABELS, KUBERNETES) is None


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        pytest.param(
            _action(action="restart_workload", kind="StatefulSet", name="redis"),
            "not allowed on StatefulSet",
            id="a kind the policy does not allow",
        ),
        pytest.param(
            _action(namespace="jenkins", name="jenkins-0"),
            "does not allow actions in jenkins",
            id="outside act_in_namespaces",
        ),
        pytest.param(
            _action(name="backend-deploy-7d9f8c6b5-zzzzz"),
            "does not exist",
            id="a target that is not there",
        ),
        pytest.param(
            _action(name="redis-7c8d9e6f5-r5s6t"),
            "no controller",
            id="a pod nothing would replace",
        ),
    ],
)
def test_actions_that_are_refused(action: ProposedAction, reason: str) -> None:
    refusal = refuse_action(action, POLICY, ALERT_LABELS, KUBERNETES)

    assert refusal is not None
    assert reason in refusal


def test_an_action_on_another_system_than_the_alert_is_refused() -> None:
    labels = {**ALERT_LABELS, "namespace": "monitoring"}

    refusal = refuse_action(_action(), POLICY, labels, KUBERNETES)

    assert refusal == "the alert is about monitoring, not task-manager"


def test_an_action_the_policy_leaves_out_is_refused() -> None:
    policy: Policy = POLICY.model_copy(
        update={
            "actions": Actions(
                restart_workload=RestartWorkload(kinds=frozenset({"Deployment"}))
            )
        }
    )

    assert refuse_action(_action(), policy, ALERT_LABELS, KUBERNETES) == (
        "delete_pod is not allowed by the policy"
    )


def test_an_action_must_fit_its_kind() -> None:
    with pytest.raises(ValidationError, match="delete_pod acts on Pod"):
        _action(kind="Deployment")
