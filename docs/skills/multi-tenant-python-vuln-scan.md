# Skill: Multi-Tenant Service Security Review (Static, Source-Level)

- **Slug:** `multi-tenant-python-vuln-scan`
- **Used by:** the Security role (reports to Infrastructure)
- **Toolsets:** terminal, file
- **Trust tier:** High-Trust internal

## Purpose

Perform a static, source-code-level security review of a multi-tenant service
built from Python, Node, and Terraform — **without** a running target, a
browser, or a sandbox. The goal is a ranked list of concrete, evidence-backed
findings: file, line, why it's exploitable, and a fix direction. This is a
read-and-reason review, not a live pentest and not a generic OWACOMP checklist
dump.

Multi-tenant isolation and LLM/tool-calling risks get first-class treatment
here, because those are the failure modes generic scanners miss.

## When to use

- Reviewing a service before a release, or auditing a repo for injection, auth,
  tenant-isolation, secret-handling, or agent/tool-calling bugs.
- After a dependency bump or a new endpoint lands, to check the delta.
- Not for finding bugs in a live URL you don't have the source for — this skill
  reads code.

## Inputs

| Input | Meaning |
|---|---|
| Repo path | The checkout to review (or a diff range for a delta review). |
| Scope | Which services/dirs are in scope (e.g. the API, the router, the infra module). |
| Threat context | Who the tenants are, what data is sensitive, what "cross-tenant" means for this system. |

## Procedure

Work in two passes: automated signal first (cheap, high-recall, noisy), then a
manual reasoning pass (the load-bearing part) that confirms or discards each
signal and finds what the tools can't.

### Pass 1 — Automated signal

Run these and treat their output as **leads, not findings**:

- **Secret scan** — `gitleaks detect --source . --redact` and
  `trufflehog filesystem .` for committed credentials, keys, tokens, and
  high-entropy strings. Also scan git history, not just the working tree.
- **SAST** — `semgrep --config auto` (or a curated ruleset) for injection sinks,
  weak crypto, unsafe deserialization, and dangerous defaults.
- **Dependency + IaC scan** — `trivy fs .` for vulnerable dependencies and
  misconfigured infra; `trivy config infrastructure/` for Terraform-specific
  issues (open security groups, public storage, disabled encryption).
- **Grep for the usual sinks** — `subprocess`/`os.system`/`eval`/`exec`,
  `child_process`/`vm`, raw SQL string-building, `pickle`/`yaml.load`,
  `dangerouslySetInnerHTML`, and disabled TLS verification.

### Pass 2 — Manual reasoning (per category)

For each category below, trace real data flow from an untrusted entry point to a
dangerous sink. A finding requires a **path**, not just a pattern match.

**1. Injection (command / SQL / template / path).**
- Follow every request parameter, header, filename, and message body to where it
  reaches a shell, a query, a template renderer, or a filesystem path.
- SQL: confirm parameterized queries everywhere; flag any f-string/concatenated
  SQL. ORMs are not automatically safe — check `.raw()` / `text()` escapes.
- Command: flag `shell=True` with any interpolated value; flag argument arrays
  built from user input without an allowlist.
- Path traversal: flag user-controlled path segments joined without
  normalization + a containment check.
- Template/SSTI: flag user input reaching a server-side template engine.

**2. AuthN / AuthZ.**
- Identify every endpoint and confirm it requires authentication — list the ones
  that don't and justify each.
- Check the **authorization** layer separately from authentication: being logged
  in is not being allowed. Look for missing object-level checks (IDOR): does the
  handler verify the caller owns the resource it's acting on, or does it trust an
  id from the request?
- Token handling: verify signature **and** expiry **and** audience/scope. Flag
  `verify=False`, `algorithms` that permit `none`, HS/RS confusion, and secrets
  shared across trust layers (session vs service vs automation tokens should have
  distinct signing keys and scopes).
- Check for auth decisions made in the client or in a header the client sets.

**3. Tenant isolation (the highest-value category for a multi-tenant service).**
- Establish how tenant identity is derived on each request (subdomain, claim,
  path segment) and confirm **every** data access is filtered by it. The classic
  bug: a query filtered by resource id but **not** by tenant id, so any tenant
  can read another's row by guessing an id.
- Check shared caches, connection pools, and background jobs — a tenant id
  captured at request time but dropped in an async worker crosses tenants
  silently.
- Check memory/vector stores and log lines for cross-tenant leakage: is
  retrieval scoped to the requesting tenant's namespace/peer? Are other tenants'
  records reachable through a broad similarity search?
- Look for "admin" or "internal" endpoints that bypass the tenant filter, and
  confirm they can't be reached with a normal token.
- Confirm the database enforces it where possible (row-level security, a
  tenant_id NOT NULL + composite keys) rather than relying only on application
  code.

**4. Secret handling.**
- No secrets in source, config committed to git, log output, or error messages
  returned to clients. Confirm secrets load from a manager (Key Vault / env
  mounted from a vault) at runtime.
- Check that debug/verbose modes don't dump env or stack frames with secrets to
  a response.
- Terraform: confirm secret values aren't hard-coded in `.tf`/`.tfvars` and
  aren't written to state in plaintext; confirm `.gitignore` covers `.env*`,
  `*.tfvars`, `*.pem`, `*.key`.

**5. LLM / agent / tool-calling risks (first-class here).**
- **Prompt injection → tool execution.** Trace whether untrusted content (a web
  page the agent fetched, a user message, a document, another agent's output)
  can reach a tool that has real-world effect (shell, file write, HTTP POST, a
  payment/email/DB action). If untrusted text can steer a privileged tool, that
  is the finding — the model is a confused deputy.
- **Tool authorization.** Each tool a role can call should be scoped to that
  role's trust tier. A customer-facing sandboxed agent must not reach
  high-trust tools or another tenant's data. Confirm the toolset grants in the
  agent profiles match the least privilege the task needs.
- **Output handling.** Model output rendered as HTML → XSS; used to build a
  shell command or query → injection; used as a file path → traversal. Treat
  model output as untrusted input to the next stage.
- **Server-side request forgery** via an agent's fetch/browse tool: can it be
  pointed at internal metadata endpoints or private hostnames? Confirm an
  allowlist / egress control.
- **Resource/cost abuse.** Unbounded loops, unbounded token spend, no per-agent
  budget or rate limit — a denial-of-wallet vector.

**6. Common web hygiene.**
- Unsafe deserialization (`pickle.loads`, `yaml.load` without `SafeLoader`,
  Node `deserialize`), SSRF, open redirects, missing rate limits on auth
  endpoints, verbose error responses that leak stack traces, and CORS set to a
  reflected/`*` origin with credentials.

### Verify before reporting

For each candidate, confirm it is reachable and exploitable by re-reading the
code path end to end. Discard pattern matches that a guard upstream already
neutralizes. A false positive in a security report costs trust; rank on
confidence and impact.

## Output format

Report findings most-severe first. For each:

```markdown
### <Severity: Critical/High/Medium/Low> — <one-line title>
- **Category:** injection | authz | tenant-isolation | secrets | llm-tool | web-hygiene
- **Location:** `path/to/file.py:123`
- **Path:** <untrusted entry point> → <intermediate> → <dangerous sink>
- **Failure scenario:** <concrete inputs/state → wrong outcome>
- **Fix direction:** <parameterize / add tenant filter / scope the token / etc.>
- **Confidence:** Confirmed | Plausible
```

Close with a short summary: counts by severity, the systemic themes (e.g.
"tenant filter enforced in the API but not in the async worker"), and the top
three fixes by risk reduction.

## Guardrails

- **Read-only review.** This skill does not modify code, exploit a live system,
  or run untrusted payloads against a target. Findings feed a separate,
  human-reviewed remediation step.
- **No secret exfiltration.** If a real secret is found committed, report its
  location and that it must be rotated — never paste the secret value into the
  report; redact it.
- **Least privilege by default.** Recommend trimming any tool grant, scope, or
  network reach that isn't needed for the task.
- **Evidence over assertion.** Every finding cites a file and line and a
  reachable path. No finding is reported on a hunch.

## Failure handling

- **A scan tool is unavailable** — note which pass you couldn't run and proceed
  with the others; do not claim coverage you didn't achieve.
- **Scope too large to review fully** — state what you reviewed and what you
  did not, and prioritize the highest-trust entry points and the tenant-isolation
  boundary first.
- **Can't determine exploitability from source alone** — report it as
  `Plausible` with the open question stated, rather than dropping it or
  overclaiming.
