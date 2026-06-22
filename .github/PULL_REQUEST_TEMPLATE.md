## What this changes

A short description of the change and why.

Closes #

## Type

- [ ] Bug fix
- [ ] Feature / enhancement
- [ ] Documentation
- [ ] Infrastructure (Terraform)

## Validation

Confirm the gates pass (see [CONTRIBUTING.md](CONTRIBUTING.md)):

- [ ] `terraform validate` passes; both `cost-optimized` and `hardened` profiles `plan` clean
- [ ] `docker compose config` is valid (if compose changed)
- [ ] Tests pass (`agents/` and `services/model-router/`)
- [ ] `gitleaks` clean — no secrets, no real hostnames or personal data
- [ ] Docs updated to match the change

## Notes

Anything reviewers should know — trade-offs, follow-ups, screenshots.
