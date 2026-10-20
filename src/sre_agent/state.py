"""The agent's durable state: today's spend and the kill switch.

Phase 1 keeps both in memory. Phase 2 moves them to DynamoDB, where the spend
is added with atomic counters and the switch is one item read before every
model call and every action.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol


class Ledger(Protocol):
    def spent_on(self, day: date) -> Decimal: ...

    def add(self, day: date, cost: Decimal) -> None: ...


class KillSwitch(Protocol):
    def is_on(self) -> bool: ...


@dataclass
class MemoryLedger:
    _spent: dict[date, Decimal] = field(default_factory=dict)

    def spent_on(self, day: date) -> Decimal:
        return self._spent.get(day, Decimal(0))

    def add(self, day: date, cost: Decimal) -> None:
        self._spent[day] = self.spent_on(day) + cost


@dataclass
class MemoryKillSwitch:
    on: bool = False

    def is_on(self) -> bool:
        return self.on


def budget_day(now: datetime) -> date:
    """The daily budget resets at 00:00 UTC, wherever the agent runs."""
    if now.utcoffset() is None:
        raise ValueError("budget_day needs a time with a time zone")
    return now.astimezone(UTC).date()
