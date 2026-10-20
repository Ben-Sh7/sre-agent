"""Talking to the model: the client interface, the price table, and a fake.

Spend is counted, not guessed: every response reports its token usage, and
code multiplies it by the prices here. A model with no prices here cannot be
used, because its spend could not be counted.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from anthropic.types import (
    ContentBlock,
    Message,
    MessageParam,
    StopReason,
    TextBlock,
    ToolUseBlock,
    Usage,
)

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Prices:
    """US dollars per million tokens."""

    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal

    def cost(self, usage: Usage) -> Decimal:
        return (
            usage.input_tokens * self.input
            + (usage.cache_creation_input_tokens or 0) * self.cache_write
            + (usage.cache_read_input_tokens or 0) * self.cache_read
            + usage.output_tokens * self.output
        ) / _MILLION

    def worst_case(self, input_tokens: int, max_tokens: int) -> Decimal:
        """The most one call could cost: every input token written to the cache,
        which costs more than a plain read, and the longest answer allowed."""
        top_input = max(self.input, self.cache_write)
        return (input_tokens * top_input + max_tokens * self.output) / _MILLION


# From the published price list. Cache writes are the 5-minute kind.
PRICES: dict[str, Prices] = {
    "claude-opus-5-5": Prices(
        Decimal("4"), Decimal("20"), Decimal("5"), Decimal("0.20")
    ),
    "claude-sonnet-5-5": Prices(
        Decimal("2"), Decimal("10"), Decimal("2.50"), Decimal("0.20")
    ),
}


class ModelClient(Protocol):
    model: str

    def create(
        self,
        *,
        system: str,
        tools: Sequence[dict[str, Any]],
        messages: Sequence[MessageParam],
        max_tokens: int,
    ) -> Message: ...


# A fake for tests and replays: it answers with prepared responses, in order.


def tool_use(call_id: str, name: str, **arguments: object) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=call_id, name=name, input=arguments)


def text(content: str) -> TextBlock:
    return TextBlock(type="text", text=content)


def response(
    *content: ContentBlock,
    stop_reason: StopReason = "tool_use",
    input_tokens: int = 2_000,
    output_tokens: int = 300,
) -> Message:
    return Message(
        id="msg_scripted",
        type="message",
        role="assistant",
        model="scripted",
        content=list(content),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


@dataclass
class ScriptedModel:
    """Plays back prepared responses and records every request it was sent."""

    responses: Iterable[Message]
    model: str = "claude-opus-5-5"
    requests: list[list[MessageParam]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._next = iter(self.responses)

    def create(
        self,
        *,
        system: str,
        tools: Sequence[dict[str, Any]],
        messages: Sequence[MessageParam],
        max_tokens: int,
    ) -> Message:
        self.requests.append(list(messages))
        try:
            return next(self._next)
        except StopIteration:
            raise AssertionError("the script has no more responses") from None
