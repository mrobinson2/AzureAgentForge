<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Security Policy

## Supported versions

This is a reference platform released under the `v1.x` line. Security fixes land on `main` and ship in the next tag. There is no long-term support branch — run a recent tag.

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Public issues are visible to everyone and give an attacker a head start.

Instead, use GitHub's private reporting:

1. Go to the **Security** tab → **Report a vulnerability** (GitHub Private Vulnerability Reporting).
2. Describe the issue, the affected component, and a reproduction if you have one.

You'll get an acknowledgement and a fix or mitigation plan. This is a portfolio/reference project maintained on a best-effort basis, so there is no formal response-time SLA — but credible reports are taken seriously and triaged promptly.

## Scope

This repository is **validated to plan, not deployed for you**. It ships infrastructure-as-code, service scaffolding, and agent role definitions — not a running, internet-exposed service. Keep that in mind when assessing impact:

- **In scope:** insecure defaults in the Terraform profiles, secret-handling mistakes in the service code or entrypoints, an example that would leak credentials if copied verbatim, or a documented setup step that weakens a deployment.
- **Out of scope:** vulnerabilities in the credited upstream projects (PaperClip, Honcho, Hermes, Cloudflared) — report those to their respective maintainers. Findings that depend on secrets you supplied yourself in a private deployment.

## What's already accounted for

- `.env.example` and `terraform.tfvars.example` contain **placeholders only** — never real secrets. Real secrets are expected to live in Azure Key Vault and be mounted at runtime.
- CI runs `gitleaks` and `terraform validate` on every push; see `.github/workflows/ci.yml`.
- For the platform's security posture and hardening options, see [docs/security.md](docs/security.md).
