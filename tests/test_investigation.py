"""The investigation loop: a full run, and every way it stops."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message, MessageParam, ThinkingBlock

from sre_agent.alert import Alert
from sre_agent.investigation import Investigation, Investigator, Limits, Outcome
from sre_agent.model import ScriptedModel, response, text, tool_use
from sre_agent.policy import load_policy
from sre_agent.prompt import NUDGE
from sre_agent.recorded import Recording
from sre_agent.state import MemoryKillSwitch, MemoryLedger, budget_day
from sre_agent.tools import READ_ONLY_TOOLS, Toolbox, ToolContext

ROOT = Path(__file__).parents[1]
RECORDING = Recording(ROOT / "tests" / "fixtures" / "redis-stuck-connection")
POLICY = load_policy(ROOT / "examples" / "policy.yaml", own_namespace="sre-agent")
BAD_POD = "backend-deploy-7d9f8c6b5-x7k2p"
ALERT = Alert.model_validate(
    {
        "fingerprint": "4f1c2a9e",
        "labels": {
            "alertname": "TaskManagerBackendErrors",
            "namespace": "task-manager",
        },
        "annotations": {"summary": "The backend is failing more than 5% of requests"},
        "startsAt": "2026-10-20T03:20:00Z",
    }
)

LOOK_AT_LOGS = tool_use(
    "toolu_1", "query_logs", namespace="task-manager", contains="Redis"
)
REPORT: dict[str, Any] = {
    "diagnosis": "One backend pod cannot reach Redis; the others are healthy.",
    "determined": True,
    "confidence": "high",
    "evidence": [{"call": 1, "quote": "Redis command timed out"}],
    "proposed_action": {
        "action": "delete_pod",
        "kind": "Pod",
        "namespace": "task-manager",
        "name": BAD_POD,
    },
    "not_checked": ["Why the connection got stuck"],
    "suggested_permanent": ["Add a Redis check to the backend's readiness probe"],
}


def submit(**changes: Any) -> Message:
    return response(tool_use("toolu_9", "submit_report", **{**REPORT, **changes}))


@dataclass
class Clock:
    now: datetime = RECORDING.recorded_at

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Setup:
    investigator: Investigator
    model: ScriptedModel
    ledger: MemoryLedger
    clock: Clock


def setup(
    *responses: Message,
    limits: Limits | None = None,
    model: ScriptedModel | None = None,
    kill_switch: bool = False,
) -> Setup:
    clock = Clock()
    context = ToolContext(
        policy=POLICY,
        metrics=RECORDING.metrics,
        logs=RECORDING.logs,
        kubernetes=RECORDING.kubernetes,
        clock=clock,
    )
    model = model or ScriptedModel(responses)
    ledger = MemoryLedger()
    investigator = Investigator(
        model=model,
        toolbox=Toolbox(READ_ONLY_TOOLS, context),
        policy=POLICY,
        kubernetes=RECORDING.kubernetes,
        ledger=ledger,
        kill_switch=MemoryKillSwitch(on=kill_switch),
        clock=clock,
        limits=limits or Limits(),
    )
    return Setup(investigator, model, ledger, clock)


def stopped(result: Investigation, reason: str) -> None:
    assert result.outcome is Outcome.STOPPED
    assert result.stopped_by is not None
    assert reason in result.stopped_by


def first_answer(request: list[MessageParam]) -> dict[str, Any]:
    """The first tool result in the last message of a request."""
    content = request[-1]["content"]
    assert isinstance(content, list)
    answer = content[0]
    assert isinstance(answer, dict)
    return dict(answer)


# A full investigation


def test_a_full_investigation() -> None:
    run = setup(response(LOOK_AT_LOGS), submit())

    result = run.investigator.investigate(ALERT)

    assert result.outcome is Outcome.REPORTED
    assert result.report is not None
    assert [item.quote for item in result.report.evidence] == [
        "Redis command timed out"
    ]
    assert result.report.dropped_evidence == ()
    assert result.report.action is not None
    assert result.report.action.name == BAD_POD
    assert [call.name for call in result.calls] == ["query_logs"]
    assert result.model_calls == 2
    # Two calls of 2,000 input and 300 output tokens at $4 and $20 a million.
    assert result.cost == Decimal("0.028")
    assert run.ledger.spent_on(budget_day(run.clock.now)) == Decimal("0.028")


def test_tool_results_reach_the_model_as_numbered_data() -> None:
    run = setup(response(LOOK_AT_LOGS), submit())

    run.investigator.investigate(ALERT)

    content = first_answer(run.model.requests[1])["content"]
    assert isinstance(content, str)
    assert content.startswith("<data-")
    assert 'call="1" tool="query_logs"' in content
    assert "Redis command timed out" in content


def test_the_history_is_only_appended_to() -> None:
    thinking = ThinkingBlock(type="thinking", thinking="", signature="sig-1")
    first = response(thinking, LOOK_AT_LOGS)
    run = setup(first, submit())

    run.investigator.investigate(ALERT)

    before, after = run.model.requests
    assert after[: len(before)] == before
    assert after[len(before)] == {"role": "assistant", "content": first.content}


def test_made_up_evidence_is_dropped_and_named() -> None:
    quotes = [{"call": 1, "quote": "connection refused by 10.0.0.9"}]
    run = setup(response(LOOK_AT_LOGS), submit(evidence=quotes))

    result = run.investigator.investigate(ALERT)

    assert result.report is not None
    assert result.report.evidence == ()
    assert "not in the result of call 1" in result.report.dropped_evidence[0]


def test_an_action_the_policy_forbids_is_dropped_and_named() -> None:
    action = {
        "action": "delete_pod",
        "kind": "Pod",
        "namespace": "jenkins",
        "name": "j-0",
    }
    run = setup(response(LOOK_AT_LOGS), submit(proposed_action=action))

    result = run.investigator.investigate(ALERT)

    assert result.report is not None
    assert result.report.action is None
    assert (
        result.report.action_refused == "the policy does not allow actions in jenkins"
    )


def test_an_invalid_report_is_sent_back_to_be_fixed() -> None:
    run = setup(response(LOOK_AT_LOGS), submit(confidence="certain"), submit())

    result = run.investigator.investigate(ALERT)

    assert result.outcome is Outcome.REPORTED
    answer = first_answer(run.model.requests[2])
    assert answer["is_error"] is True
    assert "submit_report is invalid: confidence" in str(answer["content"])


def test_a_model_that_stops_talking_is_asked_for_the_report() -> None:
    run = setup(response(text("It is Redis."), stop_reason="end_turn"), submit())

    result = run.investigator.investigate(ALERT)

    assert result.outcome is Outcome.REPORTED
    assert run.model.requests[1][-1] == {"role": "user", "content": NUDGE}


# Every way it stops


def test_the_kill_switch_stops_it_before_any_call() -> None:
    run = setup(submit(), kill_switch=True)

    stopped(run.investigator.investigate(ALERT), "kill switch")
    assert run.model.requests == []


def test_it_stops_at_the_model_call_limit() -> None:
    run = setup(*[response(LOOK_AT_LOGS)] * 5, limits=Limits(max_model_calls=3))

    result = run.investigator.investigate(ALERT)

    stopped(result, "3 model calls")
    assert result.model_calls == 3


def test_it_stops_at_the_time_limit() -> None:
    class SlowModel(ScriptedModel):
        def create(
            self,
            *,
            system: str,
            tools: Sequence[dict[str, Any]],
            messages: Sequence[MessageParam],
            max_tokens: int,
        ) -> Message:
            clock.now += timedelta(minutes=3)
            return super().create(
                system=system, tools=tools, messages=messages, max_tokens=max_tokens
            )

    model = SlowModel([response(LOOK_AT_LOGS)] * 5)
    run = setup(model=model)
    clock = run.clock

    result = run.investigator.investigate(ALERT)

    stopped(result, "time limit")
    assert result.model_calls == 2


def test_it_will_not_make_a_call_that_could_cross_the_investigation_budget() -> None:
    # One call could cost 16,000 output tokens at $20 a million: over $0.32.
    run = setup(submit(), limits=Limits(max_cost_per_investigation=Decimal("0.30")))

    stopped(run.investigator.investigate(ALERT), "per investigation")
    assert run.model.requests == []


def test_it_will_not_make_a_call_that_could_cross_the_daily_budget() -> None:
    run = setup(submit())
    run.ledger.add(budget_day(run.clock.now), Decimal("4.80"))

    stopped(run.investigator.investigate(ALERT), "per day")
    assert run.model.requests == []


def test_it_stops_when_the_tool_calls_are_used_up() -> None:
    two_calls = response(
        LOOK_AT_LOGS, tool_use("toolu_2", "list_resources", kind="Node")
    )
    run = setup(two_calls, submit(), limits=Limits(max_tool_calls=1))

    result = run.investigator.investigate(ALERT)

    assert result.calls[1].result.is_error
    assert "every tool call is used up" in result.calls[1].result.text


def test_a_refusal_stops_it() -> None:
    run = setup(response(stop_reason="refusal"))

    stopped(run.investigator.investigate(ALERT), "the model declined")


def test_a_cut_off_answer_stops_it() -> None:
    run = setup(response(text("..."), stop_reason="max_tokens"))

    stopped(run.investigator.investigate(ALERT), "max_tokens")


def test_an_api_error_stops_it() -> None:
    class BrokenModel(ScriptedModel):
        def create(
            self,
            *,
            system: str,
            tools: Sequence[dict[str, Any]],
            messages: Sequence[MessageParam],
            max_tokens: int,
        ) -> Message:
            request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.APIConnectionError(request=request)

    run = setup(model=BrokenModel([]))

    stopped(run.investigator.investigate(ALERT), "APIConnectionError")


def test_a_model_without_prices_cannot_be_used() -> None:
    with pytest.raises(ValueError, match="no prices for claude-haiku-5-5"):
        setup(model=ScriptedModel([], model="claude-haiku-5-5"))
