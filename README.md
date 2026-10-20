# sre-agent

An AI agent that investigates Prometheus alerts with read-only tools, posts a diagnosis and a proposed fix to Slack, and runs the fix only after a human approves it.

**Status:** phase 1 - the investigator, built and tested on a laptop. Start with [docs/design.md](docs/design.md).

It runs in the EKS cluster of [aws-eks-cicd-pipeline](https://github.com/Ben-Sh7/aws-eks-cicd-pipeline). That repository's `terraform apply` creates everything the agent needs in AWS and in the cluster; this one holds the agent itself - its code, tests, image and Helm chart.

## Development

Needs [uv](https://docs.astral.sh/uv/) and [gitleaks](https://github.com/gitleaks/gitleaks).

```bash
uv sync                      # the project and its development tools
uv run pre-commit install    # secret scan, lock check, audit, ruff and mypy before every commit
uv run pytest                # the tests
```
