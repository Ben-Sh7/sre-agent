"""A policy can only tighten what the code allows."""

import copy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sre_agent.policy import (
    MAX_POLICY_BYTES,
    PROTECTED_NAMESPACES,
    Policy,
    PolicyError,
    load_policy,
)

EXAMPLE = Path(__file__).parents[1] / "examples" / "policy.yaml"
AGENT_NAMESPACE = "sre-agent"

VALID: dict[str, Any] = {
    "version": 1,
    "scope": {
        "investigate_namespaces": ["task-manager", "jenkins", "argocd", "monitoring"],
        "act_in_namespaces": ["task-manager"],
    },
    "actions": {"restart_workload": {"kinds": ["Deployment"]}, "delete_pod": {}},
    "limits": {
        "max_actions_per_incident": 1,
        "max_actions_per_day": 5,
        "approval_expires_after": "15m",
    },
    "guidance": "The HPA owns the backend replica count. Never suggest a number.",
}


def _with(path: str, value: Any) -> dict[str, Any]:
    """VALID with the key at a dotted path, like 'limits.max_actions_per_day', set."""
    data = copy.deepcopy(VALID)
    *parents, key = path.split(".")
    node = data
    for name in parents:
        node = node[name]
    node[key] = value
    return data


def test_valid_policy() -> None:
    policy = Policy.model_validate(VALID)

    assert policy.scope.act_in_namespaces == {"task-manager"}
    assert policy.actions.restart_workload is not None
    assert policy.actions.restart_workload.kinds == {"Deployment"}
    assert policy.limits.approval_expires_after == timedelta(minutes=15)


def test_an_action_left_out_is_not_allowed() -> None:
    data = copy.deepcopy(VALID)
    del data["actions"]["delete_pod"]

    assert Policy.model_validate(data).actions.delete_pod is None


@pytest.mark.parametrize("namespace", sorted(PROTECTED_NAMESPACES))
def test_never_acts_in_a_protected_namespace(namespace: str) -> None:
    data = _with("scope.act_in_namespaces", [namespace])
    data["scope"]["investigate_namespaces"].append(namespace)

    with pytest.raises(ValidationError, match="the agent never acts in"):
        Policy.model_validate(data)


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        pytest.param("limts", {}, "Extra inputs are not permitted", id="misspelt key"),
        pytest.param("version", 2, "Input should be 1", id="unknown version"),
        pytest.param(
            "scope.act_in_namespaces",
            ["payments"],
            "must also be investigated",
            id="acts where it does not look",
        ),
        pytest.param(
            "scope.investigate_namespaces",
            [],
            "at least 1 item",
            id="nothing to investigate",
        ),
        pytest.param(
            "scope.investigate_namespaces",
            ["task-manager", False],
            "valid string",
            id="unquoted no in YAML",
        ),
        pytest.param(
            "scope.investigate_namespaces",
            ["Task_Manager"],
            "should match pattern",
            id="not a namespace name",
        ),
        pytest.param(
            "actions.scale_to_zero",
            {},
            "Extra inputs are not permitted",
            id="action outside the catalog",
        ),
        pytest.param(
            "actions.restart_workload.kinds",
            ["DaemonSet"],
            "'Deployment' or 'StatefulSet'",
            id="kind outside the catalog",
        ),
        pytest.param(
            "actions.delete_pod",
            None,
            "write {} to allow this action",
            id="empty action entry",
        ),
        pytest.param(
            "limits.max_actions_per_incident",
            2,
            "less than or equal to 1",
            id="more than one action per incident",
        ),
        pytest.param(
            "limits.max_actions_per_day",
            11,
            "less than or equal to 10",
            id="above the daily ceiling",
        ),
        pytest.param(
            "limits.max_actions_per_day",
            "5",
            "valid integer",
            id="number written as text",
        ),
        pytest.param(
            "limits.approval_expires_after",
            "2h",
            "whole minutes",
            id="expiry not in minutes",
        ),
        pytest.param(
            "limits.approval_expires_after",
            "61m",
            "less than or equal to",
            id="expiry above the ceiling",
        ),
        pytest.param(
            "guidance",
            "x" * 4001,
            "at most 4000 characters",
            id="guidance too long",
        ),
    ],
)
def test_rejected(path: str, value: Any, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        Policy.model_validate(_with(path, value))


# Loading the file


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_example_policy_loads() -> None:
    policy = load_policy(EXAMPLE, own_namespace=AGENT_NAMESPACE)

    assert policy.scope.act_in_namespaces == {"task-manager"}
    assert "HPA" in policy.guidance


def test_never_acts_in_its_own_namespace() -> None:
    with pytest.raises(PolicyError, match="its own namespace, task-manager"):
        load_policy(EXAMPLE, own_namespace="task-manager")


def test_a_broken_rule_names_where_it_is(tmp_path: Path) -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "max_actions_per_day: 5", "max_actions_per_day: 11"
    )

    with pytest.raises(PolicyError, match=r"limits\.max_actions_per_day: Input should"):
        load_policy(_file(tmp_path, text), own_namespace=AGENT_NAMESPACE)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        pytest.param("", "the whole file", id="empty file"),
        pytest.param("scope: [unclosed", "not valid YAML", id="not YAML"),
        pytest.param(
            "!!python/object/apply:os.system ['echo hacked']",
            "not valid YAML",
            id="tag that would run a command",
        ),
        pytest.param("x" * (MAX_POLICY_BYTES + 1), "is over", id="too big"),
    ],
)
def test_bad_file_is_rejected(tmp_path: Path, text: str, reason: str) -> None:
    with pytest.raises(PolicyError, match=reason):
        load_policy(_file(tmp_path, text), own_namespace=AGENT_NAMESPACE)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="cannot read"):
        load_policy(tmp_path / "nowhere.yaml", own_namespace=AGENT_NAMESPACE)
