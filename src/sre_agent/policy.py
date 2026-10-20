"""The policy: where the agent may look, and what it may do.

A policy can only tighten what this code allows. One that asks for more than
the ceilings below is rejected, and the agent does not start.
"""

import re
from datetime import timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    field_validator,
    model_validator,
)

# The platform's own machinery: never acted in, whatever a policy says.
PROTECTED_NAMESPACES = frozenset({"argocd", "jenkins", "kube-system", "monitoring"})

# Ceilings a policy may lower but never raise.
MAX_ACTIONS_PER_INCIDENT = 1
MAX_ACTIONS_PER_DAY = 10
MIN_APPROVAL_EXPIRY = timedelta(minutes=1)
MAX_APPROVAL_EXPIRY = timedelta(minutes=60)
# The guidance goes into every prompt, so its length is part of the cost.
MAX_GUIDANCE_CHARS = 4000

# A Kubernetes namespace name. Strict, because YAML reads an unquoted `no` as
# False: that must fail, not become the namespace "False".
Namespace = Annotated[
    str,
    Strict(),
    StringConstraints(max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"),
]

WorkloadKind = Literal["Deployment", "StatefulSet"]

_MINUTES = re.compile(r"([1-9][0-9]{0,3})m")


def _minutes(value: object) -> timedelta:
    """Read a duration written as whole minutes, like '15m'."""
    match = _MINUTES.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("write the duration in whole minutes, like '15m'")
    return timedelta(minutes=int(match[1]))


class _Model(BaseModel):
    # A misspelt key is an error, not a rule that is silently ignored.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scope(_Model):
    investigate_namespaces: Annotated[frozenset[Namespace], Field(min_length=1)]
    act_in_namespaces: frozenset[Namespace]

    @model_validator(mode="after")
    def _act_only_where_allowed(self) -> Self:
        outside = sorted(self.act_in_namespaces - self.investigate_namespaces)
        if outside:
            raise ValueError(f"act_in_namespaces must also be investigated: {outside}")
        protected = sorted(self.act_in_namespaces & PROTECTED_NAMESPACES)
        if protected:
            raise ValueError(f"the agent never acts in {protected}")
        return self


class RestartWorkload(_Model):
    kinds: Annotated[frozenset[WorkloadKind], Field(min_length=1)]


class DeletePod(_Model):
    """Deletes one pod that has a controller, which replaces it."""


class Actions(_Model):
    """The catalog. An action the policy leaves out is not allowed."""

    restart_workload: RestartWorkload | None = None
    delete_pod: DeletePod | None = None

    @field_validator("restart_workload", "delete_pod", mode="before")
    @classmethod
    def _no_empty_entry(cls, value: object) -> object:
        if value is None:
            raise ValueError("write {} to allow this action, or leave it out")
        return value


class Limits(_Model):
    max_actions_per_incident: Annotated[
        int, Strict(), Field(ge=0, le=MAX_ACTIONS_PER_INCIDENT)
    ]
    max_actions_per_day: Annotated[int, Strict(), Field(ge=0, le=MAX_ACTIONS_PER_DAY)]
    approval_expires_after: Annotated[
        timedelta,
        BeforeValidator(_minutes),
        Field(ge=MIN_APPROVAL_EXPIRY, le=MAX_APPROVAL_EXPIRY),
    ]


class Policy(_Model):
    version: Literal[1]
    scope: Scope
    actions: Actions
    limits: Limits
    # For the model only: context, not enforcement.
    guidance: Annotated[
        str, Strict(), StringConstraints(max_length=MAX_GUIDANCE_CHARS)
    ] = ""
