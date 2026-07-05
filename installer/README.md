<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../docs/assets/azureagentforge-icon-dark.png">
    <img alt="AzureAgentForge" src="../docs/assets/azureagentforge-icon-light.png" width="80">
  </picture>
</p>

# Forge Console — turnkey deployment GUI

A local web console that takes AzureAgentForge from `git clone` to a running
Azure deployment without hand-editing Terraform files or memorising the
command sequence.

```bash
./forge
```

That's the whole quickstart. The launcher creates a private virtualenv on
first run, starts the console on `http://127.0.0.1:8321`, and opens your
browser with a session-token URL.

## What it does

| Step | Behind the scenes |
|---|---|
| **1 — Prerequisites** | Detects `terraform`, `az` (and login state), Docker; lists your Azure subscriptions |
| **2 — Configure** | Renders `terraform.tfvars` from a form (preview before write) and drops a local-state `backend_override.tf`, so a first deploy needs no pre-provisioned state storage |
| **3 — Deploy** | Streams `terraform init / validate / plan / apply` live into a terminal pane. Apply only ever runs the saved plan file, and both apply and destroy require typing the environment name |
| **4 — Local path** | Runs the Docker Compose working slice (Postgres + model-router) for people who want to explore before touching Azure |

## Security model

This is a localhost tool that can run `terraform apply`, so it is built like
one:

- Binds to `127.0.0.1` only — never expose it on a network interface.
- Every state-changing request needs a per-session token embedded in the
  startup URL; a malicious web page in another tab cannot fabricate requests to
  the console (no token, and cross-origin requests are rejected outright).
- Steps are a fixed allowlist (`init`, `validate`, `plan`, `apply`, …). The
  browser never passes commands, only step names.
- `apply` and `destroy` require typed confirmation of the environment name.
- `apply` is **destroy-aware**: the console inspects the saved plan
  (`terraform show -json`) and, if it would delete or replace any resource,
  blocks behind a second, distinct approval that lists the affected resources
  and requires typing `approve-destroy`. Adds and in-place changes apply
  normally. Apply always runs the saved plan only, so what you approved is
  exactly what runs.
- One step runs at a time; output is captured and streamed via SSE.

## State backend

The console defaults to **local Terraform state** (`backend_override.tf`)
because that's the only zero-prerequisite option. Local state lives on your
machine and is git-ignored. For team or long-lived use, migrate to the
azurerm backend: fill in `infrastructure/environments/dev/backend.tf`,
delete `backend_override.tf`, and run `terraform init -migrate-state`.

## Without the GUI

Everything the console does maps to plain commands — see
[docs/getting-started.md](../docs/getting-started.md) Path B. The console is
a convenience layer, not a requirement.

## Development

```bash
.forge-venv/bin/pip install pytest httpx
.forge-venv/bin/python -m pytest installer/tests -q
```

The test suite is fully offline: it stubs subprocesses with the Python
interpreter and exercises validation, command construction, the runner, and
the API guards (token, origin, confirmation, allowlist).
