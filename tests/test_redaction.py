"""Secrets and email addresses never leave the agent; ordinary log lines do, intact."""

import time

import pytest

from sre_agent.redaction import redact

# Fake secrets, assembled from pieces so that no whole secret sits in the
# source for gitleaks to find. None of them is valid anywhere.
AWS_KEY_ID = "AKIA" + "Q3EXAMPLE7KEY5AB"
AWS_SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY"
BASIC_CREDENTIALS = "dXNlcj" + "pwYXNz"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0In0" + ".c2lnbmF0dXJl"
GITHUB_TOKEN = "ghp" + "_" + "A1b2C3d4E5" * 4
SLACK_TOKEN = "xox" + "b-" + "1234567890-ABCDEFGHIJ"
SLACK_WEBHOOK = "https://hooks.slack.com/services/" + "T000/B000/" + "XXXXXXXX"
ANTHROPIC_KEY = "sk-" + "ant-" + "api03-abcDEF123456"
PRIVATE_KEY = (
    "-----BEGIN "
    + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END "
    + "RSA PRIVATE KEY-----"
)

REDACTED_CASES = [
    pytest.param(f"key {AWS_KEY_ID} leaked", "key [REDACTED] leaked", id="AWS key ID"),
    pytest.param(
        f"export AWS_ACCESS_KEY_ID={AWS_KEY_ID} done",
        "export AWS_ACCESS_KEY_ID=[REDACTED] done",
        id="AWS key ID in a variable",
    ),
    pytest.param(f"session={JWT};", "session=[REDACTED];", id="JWT"),
    pytest.param(
        "Authorization: Bearer abc.DEF-123_xyz",
        "Authorization: Bearer [REDACTED]",
        id="Authorization header",
    ),
    pytest.param(
        f"curl -H 'Authorization: Basic {BASIC_CREDENTIALS}' https://x",
        "curl -H 'Authorization: Basic [REDACTED]' https://x",
        id="Basic credentials",
    ),
    pytest.param(
        "sent bearer 0123456789abcdef to the api",
        "sent bearer [REDACTED] to the api",
        id="bearer token in a sentence",
    ),
    pytest.param(
        "postgres://app:s3cr3t-Pa55@db.internal:5432/tasks",
        "postgres://app:[REDACTED]@db.internal:5432/tasks",
        id="password in a connection string",
    ),
    pytest.param(
        "DB_PASSWORD=hunter2 next", "DB_PASSWORD=[REDACTED] next", id="key=value"
    ),
    pytest.param('password: "hunter 2"', "password: [REDACTED]", id="quoted value"),
    pytest.param(
        '{"client_secret": "abc123"}', '{"client_secret": [REDACTED]}', id="JSON field"
    ),
    pytest.param(
        f"aws_secret_access_key = {AWS_SECRET}",
        "aws_secret_access_key = [REDACTED]",
        id="AWS secret key",
    ),
    pytest.param(f"token={GITHUB_TOKEN}", "token=[REDACTED]", id="GitHub token"),
    pytest.param(f"slack {SLACK_TOKEN}", "slack [REDACTED]", id="Slack token"),
    pytest.param(
        f"POST {SLACK_WEBHOOK} 200", "POST [REDACTED] 200", id="Slack webhook"
    ),
    pytest.param(
        f"x-api-key: {ANTHROPIC_KEY}", "x-api-key: [REDACTED]", id="Anthropic key"
    ),
    pytest.param(
        "user ben@example.com logged in",
        "user [REDACTED] logged in",
        id="email address",
    ),
    pytest.param(
        f"key:\n{PRIVATE_KEY}\nend", "key:\n[REDACTED]\nend", id="private key"
    ),
    pytest.param(
        "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBg",
        "[REDACTED]",
        id="private key cut off before its end",
    ),
]


@pytest.mark.parametrize(("text", "expected"), REDACTED_CASES)
def test_redacted(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(("text", "expected"), REDACTED_CASES)
def test_redacting_twice_changes_nothing(text: str, expected: str) -> None:
    assert redact(redact(text)) == expected


@pytest.mark.parametrize(
    "line",
    [
        "Readiness probe failed: HTTP probe failed with statuscode: 503",
        "connect ECONNREFUSED 10.0.21.14:5432",
        "Redis command timed out",
        "0/3 nodes are available: 1 node(s) had volume node affinity conflict",
        "secretName: app-secret",
        "valueFrom: secretKeyRef: {name: app-secret, key: DB_PASSWORD}",
        'sre_agent_tokens_total{type="input"} 1234',
        "max_tokens: 16000",
        "the bearer of bad news",
    ],
)
def test_ordinary_lines_are_kept(line: str) -> None:
    assert redact(line) == line


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a" * 200_000, id="one long word"),
        pytest.param("eyJ" * 70_000, id="JWT starts with no end"),
        pytest.param("x@" * 100_000, id="at signs"),
        pytest.param("password" * 25_000, id="key words with no value"),
        pytest.param("-----BEGIN " + "PRIVATE KEY-----" + "A" * 200_000, id="huge key"),
    ],
)
def test_crafted_text_cannot_stall_it(text: str) -> None:
    start = time.perf_counter()
    redact(text)

    assert time.perf_counter() - start < 2.0
