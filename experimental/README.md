<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Roadmap

This directory holds design-target reference material that is intentionally **NOT deployed** and **NOT part of the runnable stack**.

Nothing here is wired into `docker-compose.yml`, the Terraform environment modules under `infrastructure/`, or any service under `services/`. CI does not validate this directory as a deployable artifact.

## Contents

| Directory | Description |
|-----------|-------------|
| [`multi-tenant/`](multi-tenant/README.md) | Reference architecture and scaffolding for multi-tenancy. Design is ~complete; implementation is partial (~20–30%) and has never been deployed. |

See each subdirectory's `README.md` for maturity status and what is and is not built.
