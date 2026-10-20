# sre-agent

An AI agent that investigates Prometheus alerts with read-only tools, posts a diagnosis and a proposed fix to Slack, and runs the fix only after a human approves it.

**Status:** design. Start with [docs/design.md](docs/design.md).

It runs on the platform in [aws-eks-cicd-pipeline](https://github.com/Ben-Sh7/aws-eks-cicd-pipeline), which holds everything that connects it to the cluster.
