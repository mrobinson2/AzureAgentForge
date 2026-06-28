# Obsidian memory interface

A two-way bridge between AzureAgentForge's **governed memory** and a local
[Obsidian](https://obsidian.md) vault. The governed-memory six-class model maps
1:1 onto Markdown-note-with-frontmatter, so an Obsidian vault *is* the UI — there
is no frontend to install.

- **`export`** projects every governed memory into `<vault>/<id>.md`.
- You curate in Obsidian (pin, confirm, dispute, demote, delete).
- **`sync`** applies those edits back to the governor, conservatively.

Implementation: [`services/memory-governor/src/governor/vault.py`](../services/memory-governor/src/governor/vault.py).
It is a thin `httpx` client over the governor's operator API plus pure
render/parse/diff — no database access and no Azure SDK.

---

## Prerequisite: the memory-governor must be deployed and reachable

`export`/`sync` talk to the governor's operator API (`GET /memory`,
`GET /memory/{id}`, `POST /memory/{id}/action`). That API is served by the
**`ca-memory-governor-<env>`** container app, which is **gated off by default**
(`memory_governor_enabled = false`) and is **not deployed in the reference `dev`
environment as shipped**. Until it is enabled, there is no endpoint to export
from.

Enable and deploy it:

1. Set `memory_governor_enabled = true` for the environment (e.g. in
   `infrastructure/environments/dev/terraform.tfvars`, or thread it through the
   pipeline like the other vars in `.github/workflows/deploy.yml`).
2. Apply (`scripts/bootstrap.sh`/Forge Console locally, or the deploy pipeline).
   This creates `ca-memory-governor-<env>` with **internal** ingress, its managed
   identity, AcrPull + Key Vault Secrets User roles, and the `governor-api-key`
   secret mount.

> The governor ingress is **internal** (VNet-only), exactly like the model
> router — it is intentionally not exposed to the public internet.

---

## Connecting from your machine

`GovernorClient.from_env()` picks a transport from environment variables, in this
order:

| Mode | Env vars | Auth header | Use when |
|---|---|---|---|
| **Auth-proxy (recommended for laptops)** | `MEMORY_API_BASE_URL`, `MEMORY_API_TOKEN` | `Authorization: Bearer <JWT>` (path prefix `/api`) | A mission-control / auth-proxy host is exposed publicly |
| **In-network** | `GOVERNOR_BASE_URL` (default `http://ca-memory-governor-dev`), `GOVERNOR_API_KEY` | `X-Governor-Key: <key>` | Running inside the VNet, or tunnelling to the internal endpoint |

The in-network API key is the `governor-api-key` Key Vault secret:

```bash
export GOVERNOR_API_KEY="$(az keyvault secret show \
  --vault-name aaf-vault-dev-kv --name governor-api-key --query value -o tsv)"
```

### Reaching the internal governor

Because `ca-memory-governor-dev` has internal ingress, a laptop cannot hit it
directly. Pick one:

- **Run from inside the VNet** — e.g. a one-off container/job in the same
  Container Apps environment (the most reliable path; the same approach used to
  probe the internal router).
- **Tunnel** to the internal FQDN (VPN, Cloudflare tunnel, or a jump host), then
  set `GOVERNOR_BASE_URL` to the reachable address.
- **Auth-proxy** — if a public mission-control host fronts the governor, set
  `MEMORY_API_BASE_URL` + `MEMORY_API_TOKEN` and skip the VNet entirely.

---

## Export: populate the vault

```bash
cd services/memory-governor/src
python -m governor.vault export ~/AAF-memory-vault
# -> exported N notes to ~/AAF-memory-vault
```

`export` writes one `<id>.md` per memory plus a hidden baseline file
(`.governor-baseline.json`) that `sync` uses to detect your edits. Open the
directory as an Obsidian vault.

Each note's frontmatter is the memory's governed fields; the body is its content.
Relations (`superseded_by`, `promotion_source_doc_id`) render as `[[wikilinks]]`,
so Obsidian's graph view works.

| Frontmatter key | Governor field |
|---|---|
| `id` | `id` |
| `class` | `memory_class` |
| `verification` | `verification_state` |
| `scope_kind` / `scope_id` | `memory_scope_kind` / `memory_scope_id` |
| `source` | `source_type` |
| `created_by` | `created_by_peer` |
| `created_at` / `last_confirmed_at` / `expires_at` | same |

Empty/None fields are omitted.

---

## Repeated exports — no duplicates

**Re-running `export` never creates duplicates.** Each note is named by the
memory's **stable governor id** (`<id>.md`), so a second export *overwrites the
same file in place*; it is idempotent. The baseline file is likewise rewritten.

Two things to know before you re-export, though:

1. **Re-export overwrites notes wholesale** — it re-renders each note from the
   current server state, so it **clobbers any local edits you have not synced
   yet**. The intended loop is **export → curate → `sync` → (optionally re-export
   to refresh)**. Always `sync` before re-exporting if you have pending edits.
2. **Export does not prune** — notes for memories that were *forgotten/deleted
   server-side* are not removed from the vault; the stale `<id>.md` lingers (it is
   stale, not a duplicate). To get a clean mirror, export into a fresh directory,
   or delete notes whose ids no longer appear in a fresh `list`. (`sync` *does*
   treat a note you delete locally as a `forget`.)

---

## Sync: apply your edits back

```bash
python -m governor.vault sync ~/AAF-memory-vault
# -> applied K change(s); M conflict(s) skipped: [...]
```

`sync` compares the vault against `.governor-baseline.json` and maps edits to
governor actions (all attributed to `actor=operator`):

| You change… | Action sent |
|---|---|
| Delete the note file | `rm` (forget the memory) |
| `class: pinned` | `pin` |
| `verification: confirmed` | `confirm` |
| `verification: disputed` | `dispute` |
| `class:` → `durable_fact` / `user_preference` / `task_scoped` / `decaying` | `demote` (to that class) |

**Conflict safety:** before applying, `sync` re-fetches current server state and
**skips any memory whose class/verification changed on the server since your
export** (listed in the output). The governor stays the source of truth — nothing
is silently clobbered. Re-`export` to pick up those server changes, then curate
again.

**Not handled:** editing a note's **body/content** is *not* synced (creating a
superseding memory from an edited body is a documented follow-on). Frontmatter is
the control surface.
