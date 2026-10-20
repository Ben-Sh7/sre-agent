"""The policy: where the agent may look, and what it may do.

A policy can only tighten what this code allows. One that asks for more than
the ceilings below is rejected, and the agent does not start.
"""

import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

# A policy is a short, hand-written file. Anything bigger is a mistake or an attack.
MAX_POLICY_BYTES = 64 * 1024

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


class PolicyError(Exception):
    """The policy cannot be used, so the agent must not start."""


def load_policy(path: Path, *, own_namespace: str) -> Policy:
    """Read, parse and check a policy file. Every problem raises PolicyError."""
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_POLICY_BYTES + 1)
    except OSError as error:
        raise PolicyError(f"cannot read the policy {path}: {error.strerror}") from error
    if len(raw) > MAX_POLICY_BYTES:
        raise PolicyError(f"the policy {path} is over {MAX_POLICY_BYTES} bytes")

    try:
        # Plain data only: safe_load refuses the tags that build objects or run code.
        data = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise PolicyError(f"the policy {path} is not valid YAML: {error}") from error

    try:
        policy = Policy.model_validate(data)
    except ValidationError as error:
        raise PolicyError(_describe(path, error)) from error

    if own_namespace in policy.scope.act_in_namespaces:
        raise PolicyError(f"the agent never acts in its own namespace, {own_namespace}")
    return policy


def _describe(path: Path, error: ValidationError) -> str:
    """One line per broken rule, naming where it is. Values are not echoed."""
    lines = [f"the policy {path} breaks {error.error_count()} rule(s):"]
    for item in error.errors(include_url=False, include_input=False):
        where = ".".join(str(part) for part in item["loc"]) or "(the whole file)"
        lines.append(f"  {where}: {item['msg']}")
    return "\n".join(lines)
