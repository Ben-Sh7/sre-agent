"""Redaction: secrets and personal data are replaced before text leaves the agent.

It runs on every tool result before the model reads it, and on everything sent
to Slack or written to the logs. A list of patterns is never complete, so this
is one layer of several: the tools never read Secrets in the first place.

Every pattern starts at a boundary and every repeat has an upper bound, so the
time a text takes grows in step with its length. Log lines are written by
outsiders, and a crafted line must not be able to stall the agent.
"""

import re
from collections.abc import Callable
from typing import Final

REDACTED: Final = "[REDACTED]"

# Keys that name a secret instead of holding one, such as secretName or
# secretKeyRef, or that count tokens, such as max_tokens. Their values help
# a diagnosis and are kept.
_NOT_A_SECRET_SUFFIXES: Final = ("name", "ref", "path", "file", "tokens", "total")

_KEY_VALUE: Final = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"([A-Za-z0-9_.-]{0,40}"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credential)"
    r"[A-Za-z0-9_.-]{0,40})"
    r"(\"?'?\s{0,5}[:=]\s{0,5})"
    r"(?!\[REDACTED\])"
    r"(\"[^\"\n]{0,1024}\"|'[^'\n]{0,1024}'|[^\s,;&\"'}\]]{1,1024})"
)


def _key_value(match: re.Match[str]) -> str:
    key, separator, _ = match.groups()
    if key.lower().endswith(_NOT_A_SECRET_SUFFIXES):
        return match[0]
    return f"{key}{separator}{REDACTED}"


_RULES: Final[
    tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...]
] = (
    # A private key block. A block cut off before its END line is redacted too.
    (
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----"
            r"(?:[\s\S]{0,20000}?-----END [A-Z ]{0,40}PRIVATE KEY-----|[\s\S]{0,20000})"
        ),
        REDACTED,
    ),
    # Tokens with a known shape: JWT, AWS key ID, GitHub, Slack, Anthropic.
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{1,4096}+\.eyJ[A-Za-z0-9_-]{1,8192}+"
            r"\.[A-Za-z0-9_-]{0,4096}+"
        ),
        REDACTED,
    ),
    (
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}(?![A-Z0-9])"),
        REDACTED,
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,255}+|github_pat_[A-Za-z0-9_]{22,255}+)"
        ),
        REDACTED,
    ),
    (re.compile(r"(?<![A-Za-z0-9-])xox[abposr]-[A-Za-z0-9-]{10,255}+"), REDACTED),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{1,255}+"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{10,255}+"), REDACTED),
    # An Authorization header: keep the scheme, drop the credentials. The scheme
    # is possessive (?+): once matched it is never retried as the credential.
    (
        re.compile(
            r"(?i)(?<![A-Za-z])(authorization[\"']?\s{0,5}[:=]\s{0,5}[\"']?"
            r"(?:(?:basic|bearer|token|digest)\s{1,5})?+)"
            r"(?!\[REDACTED\])[^\s\"',;]{1,4096}+"
        ),
        rf"\1{REDACTED}",
    ),
    (
        re.compile(
            r"(?i)(?<![A-Za-z])(bearer\s{1,5})(?!\[REDACTED\])[A-Za-z0-9._~+/=-]{8,4096}+"
        ),
        rf"\1{REDACTED}",
    ),
    # A password inside a URL: scheme://user:password@host
    (
        re.compile(
            r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]{0,31}://[^\s:/@]{1,256}:)"
            r"(?!\[REDACTED\])[^\s@/]{1,256}(@)"
        ),
        rf"\1{REDACTED}\2",
    ),
    (_KEY_VALUE, _key_value),
    (
        re.compile(
            r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}"
            r"(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}(?![A-Za-z0-9-])"
        ),
        REDACTED,
    ),
)


def redact(text: str) -> str:
    """Return the text with every secret and email address replaced."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
