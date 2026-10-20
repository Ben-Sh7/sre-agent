"""One investigation: the model loop, inside every limit.

Before each model call, code checks the kill switch, the call count, the clock
and both budgets, and stops at the first limit it reaches. The model asks for
tools; code validates and runs them. The only way to finish is submit_report,
whose evidence and action code checks before anyone sees them.

The conversation is only ever appended to. The model's responses go back
exactly as they came, thinking blocks included, and the tool list is fixed
from the first call: editing either would invalidate the model's earlier
reasoning and the prompt cache.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import anthropic
from anthropic.types import Message, MessageParam, ToolResultBlockParam
from pydantic import ValidationError

from sre_agent.alert import Alert
from sre_agent.model import PRICES, ModelClient, Prices
from sre_agent.policy import Policy
from sre_agent.prompt import NUDGE, DataTags, first_message, system_prompt
from sre_agent.report import (
    SUBMIT_REPORT,
    SUBMIT_REPORT_TOOL,
    CheckedReport,
    Report,
    ToolCall,
    check_report,
)
from sre_agent.sources import Kubernetes
from sre_agent.state import KillSwitch, Ledger, budget_day
from sre_agent.tools import Toolbox, ToolResult
from sre_agent.tools.base import describe_errors

# Cautious: English runs nearer four characters to a token. The real count
# arrives with each response and replaces the estimate.
CHARS_PER_TOKEN = 2


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 8
    max_tool_calls: int = 24
    max_duration: timedelta = timedelta(minutes=5)
    max_cost_per_investigation: Decimal = Decimal("1.00")
    max_cost_per_day: Decimal = Decimal("5.00")
    max_tokens: int = 16_000


class Outcome(StrEnum):
    REPORTED = "reported"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Investigation:
    outcome: Outcome
    stopped_by: str | None
    report: CheckedReport | None
    calls: tuple[ToolCall, ...]
    model_calls: int
    cost: Decimal


@dataclass
class _Run:
    """What one investigation has done so far."""

    started: datetime
    messages: list[MessageParam]
    calls: dict[int, ToolCall] = field(default_factory=dict)
    model_calls: int = 0
    cost: Decimal = Decimal(0)
    # Prompt size: exact as of the last response, plus an estimate for what
    # was appended since.
    known_tokens: int = 0
    new_chars: int = 0

    def estimated_input_tokens(self) -> int:
        return self.known_tokens + self.new_chars // CHARS_PER_TOKEN + 1

    def append(self, message: MessageParam, chars: int) -> None:
        self.messages.append(message)
        self.new_chars += chars


@dataclass
class Investigator:
    model: ModelClient
    toolbox: Toolbox
    policy: Policy
    kubernetes: Kubernetes
    ledger: Ledger
    kill_switch: KillSwitch
    clock: Callable[[], datetime]
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self) -> None:
        prices = PRICES.get(self.model.model)
        if prices is None:
            raise ValueError(
                f"no prices for {self.model.model}: its spend could not be counted"
            )
        self._prices: Prices = prices
        self._system = system_prompt(self.policy)
        self._tools = [*self.toolbox.definitions(), SUBMIT_REPORT_TOOL]

    def investigate(self, alert: Alert) -> Investigation:
        tags = DataTags()
        opening = first_message(alert, tags)
        run = _Run(
            started=self.clock(),
            messages=[{"role": "user", "content": opening}],
            new_chars=len(self._system) + len(json.dumps(self._tools)) + len(opening),
        )
        while True:
            limit = self._limit_reached(run)
            if limit is not None:
                return self._stop(run, limit)
            try:
                message = self.model.create(
                    system=self._system,
                    tools=self._tools,
                    messages=run.messages,
                    max_tokens=self.limits.max_tokens,
                )
            except anthropic.APIError as error:
                # The SDK has already retried what is worth retrying.
                return self._stop(run, f"model API error: {type(error).__name__}")
            self._count(run, message)
            run.messages.append({"role": "assistant", "content": message.content})

            if message.stop_reason == "tool_use":
                report = self._handle_tools(run, message, alert, tags)
                if report is not None:
                    return Investigation(
                        Outcome.REPORTED,
                        None,
                        report,
                        tuple(run.calls.values()),
                        run.model_calls,
                        run.cost,
                    )
            elif message.stop_reason == "end_turn":
                run.append({"role": "user", "content": NUDGE}, len(NUDGE))
            elif message.stop_reason == "refusal":
                category = (
                    message.stop_details.category if message.stop_details else None
                )
                return self._stop(
                    run, f"the model declined ({category or 'no category'})"
                )
            elif message.stop_reason == "max_tokens":
                return self._stop(run, "the answer was cut off at max_tokens")
            else:
                return self._stop(run, f"unexpected stop reason {message.stop_reason}")

    def _limit_reached(self, run: _Run) -> str | None:
        limits = self.limits
        if self.kill_switch.is_on():
            return "the kill switch is on"
        if run.model_calls >= limits.max_model_calls:
            return f"the limit of {limits.max_model_calls} model calls"
        now = self.clock()
        if now - run.started >= limits.max_duration:
            return f"the time limit of {limits.max_duration}"
        worst = self._prices.worst_case(run.estimated_input_tokens(), limits.max_tokens)
        if run.cost + worst > limits.max_cost_per_investigation:
            return (
                f"the budget of ${limits.max_cost_per_investigation} per investigation"
            )
        if self.ledger.spent_on(budget_day(now)) + worst > limits.max_cost_per_day:
            return f"the budget of ${limits.max_cost_per_day} per day"
        return None

    def _count(self, run: _Run, message: Message) -> None:
        cost = self._prices.cost(message.usage)
        run.model_calls += 1
        run.cost += cost
        self.ledger.add(budget_day(self.clock()), cost)
        usage = message.usage
        run.known_tokens = (
            usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
            + usage.output_tokens
        )
        run.new_chars = 0

    def _handle_tools(
        self, run: _Run, message: Message, alert: Alert, tags: DataTags
    ) -> CheckedReport | None:
        """Answer every tool call in the message. A valid report ends the run."""
        blocks = [block for block in message.content if block.type == "tool_use"]
        report_block = next((b for b in blocks if b.name == SUBMIT_REPORT), None)
        if report_block is not None:
            try:
                report = Report.model_validate(report_block.input)
            except ValidationError as error:
                # Every tool_use needs an answer. The report's says what to fix;
                # the others were not run, because a report ends the investigation.
                invalid = f"submit_report is invalid: {describe_errors(error)}"
                self._answer(
                    run,
                    [
                        (block.id, invalid, True)
                        if block is report_block
                        else (block.id, "not run: it came with submit_report", True)
                        for block in blocks
                    ],
                )
                return None
            return check_report(
                report, run.calls, self.policy, alert.labels, self.kubernetes
            )

        answers = []
        for block in blocks:
            arguments: dict[str, Any] = (
                block.input if isinstance(block.input, dict) else {}
            )
            if len(run.calls) >= self.limits.max_tool_calls:
                result = ToolResult(
                    "not run: every tool call is used up. Call submit_report now.",
                    is_error=True,
                )
            else:
                result = self.toolbox.call(block.name, arguments)
            number = len(run.calls) + 1
            run.calls[number] = ToolCall(number, block.name, arguments, result)
            answers.append(
                (
                    block.id,
                    tags.wrap(result.text, call=str(number), tool=block.name),
                    result.is_error,
                )
            )
        self._answer(run, answers)
        return None

    def _answer(self, run: _Run, answers: list[tuple[str, str, bool]]) -> None:
        results: list[ToolResultBlockParam] = [
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": content,
                "is_error": is_error,
            }
            for call_id, content, is_error in answers
        ]
        run.append(
            {"role": "user", "content": results},
            sum(len(content) for _, content, _ in answers),
        )

    def _stop(self, run: _Run, limit: str) -> Investigation:
        return Investigation(
            Outcome.STOPPED,
            limit,
            None,
            tuple(run.calls.values()),
            run.model_calls,
            run.cost,
        )
