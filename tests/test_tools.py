"""The read-only tools, run against a recorded incident."""

from pathlib import Path

import pytest

from sre_agent.policy import load_policy
from sre_agent.recorded import Recording
from sre_agent.tools import READ_ONLY_TOOLS, Toolbox, ToolContext
from sre_agent.tools.base import MAX_RESULT_CHARS, cap

ROOT = Path(__file__).parents[1]
RECORDING = ROOT / "tests" / "fixtures" / "redis-stuck-connection"
ERRORS_BY_POD = (
    "sum by (pod) (rate(http_request_duration_seconds_count"
    '{namespace="task-manager", status=~"5.."}[5m]))'
)
BAD_POD = "backend-deploy-7d9f8c6b5-x7k2p"


@pytest.fixture
def toolbox() -> Toolbox:
    recording = Recording(RECORDING)
    policy = load_policy(ROOT / "examples" / "policy.yaml", own_namespace="sre-agent")
    context = ToolContext(
        policy=policy,
        metrics=recording.metrics,
        logs=recording.logs,
        kubernetes=recording.kubernetes,
        clock=lambda: recording.recorded_at,
    )
    return Toolbox(READ_ONLY_TOOLS, context)


def ok(toolbox: Toolbox, tool: str, /, **arguments: object) -> str:
    result = toolbox.call(tool, arguments)
    assert not result.is_error, result.text
    return result.text


def refused(toolbox: Toolbox, tool: str, /, **arguments: object) -> str:
    result = toolbox.call(tool, arguments)
    assert result.is_error, result.text
    return result.text


# Every call


def test_every_tool_offers_a_closed_schema(toolbox: Toolbox) -> None:
    definitions = toolbox.definitions()

    assert {d["name"] for d in definitions} == {tool.name for tool in READ_ONLY_TOOLS}
    for definition in definitions:
        assert definition["description"]
        assert definition["input_schema"]["additionalProperties"] is False


def test_a_tool_that_does_not_exist_is_refused(toolbox: Toolbox) -> None:
    assert "no tool named" in refused(toolbox, "run_shell", command="ls")


def test_unknown_arguments_are_refused(toolbox: Toolbox) -> None:
    text = refused(toolbox, "query_logs", namespace="task-manager", sql="DROP")

    assert "Extra inputs are not permitted" in text


def test_a_long_result_is_cut_and_says_so() -> None:
    text = cap("x" * (MAX_RESULT_CHARS + 1000))

    assert len(text) == MAX_RESULT_CHARS
    assert text.endswith("ask a narrower question]")
    assert cap("short") == "short"


# query_metrics


def test_instant_query(toolbox: Toolbox) -> None:
    text = ok(toolbox, "query_metrics", promql=ERRORS_BY_POD)

    assert f'{{pod="{BAD_POD}"}} 0.31' in text


def test_range_query(toolbox: Toolbox) -> None:
    text = ok(toolbox, "query_metrics", promql=ERRORS_BY_POD, range_minutes=30)

    assert f'{{pod="{BAD_POD}"}} min 0 max 0.31 last 0.31' in text


def test_a_query_that_was_not_recorded(toolbox: Toolbox) -> None:
    assert "not recorded" in refused(toolbox, "query_metrics", promql="up")


def test_a_step_under_30_seconds_is_refused(toolbox: Toolbox) -> None:
    text = refused(
        toolbox, "query_metrics", promql="up", range_minutes=5, step_seconds=5
    )

    assert "step_seconds" in text


# query_logs


def test_logs_narrowed_to_one_text(toolbox: Toolbox) -> None:
    lines = ok(toolbox, "query_logs", namespace="task-manager", contains="Redis")

    assert lines.splitlines() == [
        f"2026-10-20T03:18:44Z {BAD_POD} Redis command timed out",
        f"2026-10-20T03:09:01Z {BAD_POD} Redis command timed out",
    ]


def test_logs_reach_back_only_as_far_as_asked(toolbox: Toolbox) -> None:
    text = ok(
        toolbox, "query_logs", namespace="task-manager", contains="Redis", minutes=60
    )

    assert "early warning" in text


def test_logs_outside_the_scope_are_refused(toolbox: Toolbox) -> None:
    text = refused(toolbox, "query_logs", namespace="kube-system")

    assert "outside the investigation scope" in text


def test_logs_are_redacted(toolbox: Toolbox) -> None:
    text = ok(toolbox, "query_logs", namespace="task-manager", contains="login")

    assert "dana@example.com" not in text
    assert "login ok for user [REDACTED]" in text


# Kubernetes objects


def test_a_pod_without_bookkeeping_or_env_values(toolbox: Toolbox) -> None:
    text = ok(
        toolbox, "get_resource", kind="Pod", namespace="task-manager", name=BAD_POD
    )

    assert "managedFields" not in text
    assert "last-applied-configuration" not in text
    assert "verbose-debugging" not in text
    assert "name: LOG_LEVEL" in text
    assert "value: '[REDACTED]'" in text
    assert "secretKeyRef" in text


@pytest.mark.parametrize("kind", ["Secret", "ConfigMap"])
def test_secrets_and_configmaps_cannot_be_read(toolbox: Toolbox, kind: str) -> None:
    text = refused(
        toolbox, "get_resource", kind=kind, namespace="task-manager", name="app-secret"
    )

    assert "kind" in text


def test_a_node_is_read_without_a_namespace(toolbox: Toolbox) -> None:
    text = ok(toolbox, "get_resource", kind="Node", name="ip-10-0-1-10")

    assert "us-east-1a" in text
    assert "sizeBytes" not in text
    assert "not namespaced" in refused(
        toolbox, "get_resource", kind="Node", namespace="task-manager", name="x"
    )


def test_list_pods(toolbox: Toolbox) -> None:
    text = ok(toolbox, "list_resources", kind="Pod", namespace="task-manager")

    assert f"{BAD_POD} Running ready 1/1 restarts 0 node ip-10-0-1-10" in text


def test_list_nodes(toolbox: Toolbox) -> None:
    text = ok(toolbox, "list_resources", kind="Node")

    assert text.splitlines() == [
        "ip-10-0-1-10 ready True zone us-east-1a",
        "ip-10-0-2-20 ready True zone us-east-1b",
    ]


def test_events_newest_first(toolbox: Toolbox) -> None:
    text = ok(toolbox, "get_events", namespace="task-manager", kind="Pod", name=BAD_POD)

    assert text.splitlines()[0].startswith("2026-10-18T10:00:20Z Normal Started")


def test_pod_logs_keep_the_last_lines(toolbox: Toolbox) -> None:
    text = ok(
        toolbox, "get_pod_logs", namespace="task-manager", pod=BAD_POD, tail_lines=2
    )

    assert text.splitlines() == [
        "2026-10-20T03:12:30Z GET /api/tasks 500 5003ms",
        "2026-10-20T03:18:44Z Redis command timed out",
    ]


def test_argocd_application(toolbox: Toolbox) -> None:
    text = ok(toolbox, "get_argocd_app", name="task-manager")

    assert "sync: Synced at revision def4567890abcdef1234" in text
    assert "health: Healthy" in text
    assert "deployed def4567890ab at 2026-10-19T21:58:00Z" in text


def test_recent_changes(toolbox: Toolbox) -> None:
    text = ok(toolbox, "get_recent_changes", namespace="task-manager")

    assert text.splitlines() == [
        "2026-10-19T22:00:00Z Deployment frontend-deploy rolled out revision 12: "
        "ecr/frontend:def4567",
        "2026-10-19T21:58:00Z ArgoCD application task-manager deployed revision "
        "def4567890ab",
    ]
