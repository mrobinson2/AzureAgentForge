<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Contributing

## Proposing changes

Open an issue before starting non-trivial work. Describe what you want to change and why. This avoids duplicate effort and keeps infrastructure changes from conflicting.

For small fixes (typos, documentation corrections, minor bug fixes), a pull request without a prior issue is fine.

Pull requests should target `main`. Keep changes focused — infrastructure changes and application logic changes in separate PRs make review faster and rollback cleaner.

## Validation gates

Every pull request must pass all of the following before merge. These are not optional.

### Terraform

Run both cost profiles:

```bash
cd infrastructure/environments/dev

terraform init
terraform validate

terraform plan -var-file=../../profiles/cost-optimized.tfvars -var-file=terraform.tfvars
terraform plan -var-file=../../profiles/hardened.tfvars     -var-file=terraform.tfvars
```

Both profiles must plan clean (no errors, no unexpected resource replacements).

### Docker Compose

```bash
docker compose config
```

Must produce valid output with no errors.

### Agent profile schema validation

```bash
cd agents
python validate_profiles.py
```

Must exit 0. This validates all YAML profiles in `agents/profiles/` against `agents/profile.schema.json`. Any new agent profile must pass before merge.

### Test suites

Agent tests:

```bash
cd agents
pip install -r requirements-dev.txt
pytest tests/
```

Router tests:

```bash
cd services/model-router
pip install -r requirements-dev.txt
pytest tests/
```

Both must pass.

### Secret scanning

```bash
gitleaks detect --source . --config .gitleaks.toml
```

Must produce zero findings. See the hard rule on secrets below.

## Code structure

```
agents/                 Agent profiles (YAML), profile schema, profile validator
  profiles/             One YAML file per agent role
  profile.schema.json   JSON Schema that all profiles are validated against
  validate_profiles.py  Validation script
  tests/                Agent-layer tests

services/               Containerized services
  model-router/         OpenAI-compatible model gateway (FastAPI)
  agent-runtime/        Hermes agent runtime
  honcho/               Self-hosted memory service
  paperclip/            Orchestrator + web UI

integrations/           Optional chat bridges
  discord/              Discord bridge (off by default)
  telegram/             Telegram bridge (off by default)

infrastructure/         Terraform
  environments/dev/     Deployable environment root
  modules/              Reusable Terraform modules (network, postgres, keyvault, etc.)
  profiles/             Cost and security profiles (.tfvars)

docs/                   Documentation
  architecture.md       System architecture and component reference
  cost.md               Cost estimates and breakdown
  getting-started.md    Deployment walkthrough
  why-azure.md          Design rationale

docker-compose.yml      Local development stack
.env.example            Environment variable template (no real values)
```

## The hard rule on secrets

Never commit secrets, credentials, API keys, connection strings, or tokens of any kind.

`.env` stays local. It is in `.gitignore` and must stay there.

If a new service or configuration needs credentials, add the variable name to `.env.example` with a placeholder value and instructions, not the real value. The same applies to `infrastructure/terraform.tfvars.example`.

If `gitleaks` flags something, do not use `--no-git` or allowlist your way around it without understanding why it fired. Fix the actual problem.

If you accidentally commit a secret, rotate it immediately. Removing it from history is a secondary concern; the priority is making the leaked credential unusable.
