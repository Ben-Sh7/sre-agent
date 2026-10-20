# sre-agent

An AI agent that investigates Prometheus alerts with read-only tools, posts a diagnosis and a proposed fix to Slack, and runs the fix only after a human approves it.

**Status:** design. Start with [docs/design.md](docs/design.md).

It runs in the EKS cluster of [aws-eks-cicd-pipeline](https://github.com/Ben-Sh7/aws-eks-cicd-pipeline). That repository's `terraform apply` creates everything the agent needs in AWS and in the cluster; this one holds the agent itself - its code, tests, image and Helm chart.
