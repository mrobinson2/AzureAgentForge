# Governed-AI Interface Pattern Library

Nine reusable interface patterns for consumer-facing AI — the surfaces that keep an assistant
honest, deferential, and auditable in front of real customers. Each is a small, dependency-free,
themeable reference implementation. Open [`index.html`](./index.html) for the living version:
**all nine patterns rendered, interactive, and reskinnable** by design tokens alone (the theme
switcher is the proof).

Vendor-neutral by construction. Every business, name, number, and outcome in the demo is
**fictional and labeled as a simulation** — no real integrations are called.

## The nine patterns

| # | Pattern | One-line contract |
|---|---|---|
| P1 | **Demo Honesty Badge** | One permanently-visible badge per simulated surface; names what's simulated and what production does. |
| P2 | **Trust Receipt** | Every consequential AI output lists *did / applied / deferred* (≥1 deferred line, or say "none"). Variants: action receipt, call summary, assembly receipt, privacy receipt. |
| P3 | **Refusal Card** | Refusal (plain) + why (a respectable rule) + human path (named person, promised time). Never a dead end. |
| P4 | **Approval Gate** | Full draft shown → Approve/Adjust/Decline → decided-state line naming what happened. `Gov.gate(el)`. |
| P5 | **Pricing-Policy Engine** | Owner's versioned policy as data → line items + **range**, never a single figure. `Gov.quote(policy, picks)`. |
| P6 | **Autonomy Policy Panel** | Exactly three tiers: autonomous (reversible, ledgered) / your approval (P4) / never—escalate (P3, enforced in code). |
| P7 | **Sealed Record** | A consequential number (quote, change order) written to a ledger at issue: ID + timestamp + basis + range + change rules. Never edited in place — superseded or extended by a NEW seal; the customer can verify any record by its ID (VERIFIED / MISMATCH). |
| P8 | **Movement Log** (queue transparency) | A customer's place in any queue shows: position, an honest *window* (never an invented date — that's a P3 refusal), the published priority rules, and a log where **every movement names its cause**. |
| P9 | **Signed Charter** (amendment asymmetry) | The owner's governance as ONE versioned, signed document every surface reads. Tightening applies instantly; loosening needs a fresh signature on a new version; never-list items are not loosenable in-product. Versions are never edited, only succeeded. |

## Composition rule of thumb

Every AI surface = **P1 always** · **P2** on every consequential output · **P3** wherever the
owner hasn't priced/ruled · **P4** before anything external · **P5** wherever money is quoted ·
**P6** declared once, enforced everywhere. And where they apply: **P7** wherever a number outlives
the conversation (the customer holds the ID) · **P8** wherever customers wait in a queue · **P9**
as the single source P6 reads from — the charter is *declared* in P9, *summarized* in P6, and
(in a real product) enforced by a runtime governance layer rather than a prompt.

## Files

| File | What it is |
|---|---|
| `index.html` | The demo page — all nine patterns, live and themeable. Doubles as the reference *consumer page*. |
| `patterns.css` | The tokens (`--gov-*`) + the pattern styles. Everything below the "THE PATTERNS" banner is copy-pastable. |
| `patterns.js` | `window.Gov.gate` (P4) and `window.Gov.quote` (P5), plus the demo-only wiring and theme switcher. |
| `check.js` | The conformance linter (11 checks C1–C11). Zero dependencies. |
| `CONFORMANCE.md` | The generated report for this directory (regenerate with the command below). |

## Theming contract

All pattern visuals hang off `--gov-*` tokens (top of `patterns.css`). Reskinning an identity =
redefining tokens under a `[data-theme="…"]` selector — **zero markup changes**. The page's theme
switcher (Neutral / Night shift / Warm trade) is the proof; the same markup renders under all three.

## Using these patterns in a new page

1. Copy the token block + the pattern rules you need from `patterns.css` (everything below the
   "THE PATTERNS" banner), and `Gov.gate` / `Gov.quote` from `patterns.js` if you use P4/P5.
2. Redefine `--gov-*` tokens to your identity.
3. Keep the markup contracts (class names + structure) so the conformance linter can verify them.

### `Gov.gate(el)` (P4)

```html
<div class="gov-gate" data-approve="✓ Sent (simulated)." data-adjust="✎ Opened for editing." data-decline="✕ Declined.">
  <p class="gg-draft">…the full draft, shown in full…</p>
  <div class="gg-actions">
    <button class="gg-approve">Approve</button>
    <button class="gg-adjust">Adjust</button>
    <button class="gg-decline">Decline</button>
  </div>
</div>
```
Every outcome replaces the action buttons with a decided-state line naming what happened.

### `Gov.quote(policy, picks)` (P5)

```js
const policy = {
  version: "v3", spread: 0.1,
  items: {
    base: { label: "Base service", kind: "base", value: 180 },
    size: { label: "Large size",   kind: "mult", value: 1.3 }
  }
};
Gov.quote(policy, { base: true, size: false }); // -> { lines: [...], low, high }
```
Deterministic; returns a **range**, never a single figure. Pair with a P2 receipt and a P4 gate.

## Conformance linter (`check.js`)

`check.js` lints *pattern-consumer pages* against eleven contract checks (C1–C11) derived from the
patterns. It is **static and heuristic**: it verifies the contracts are present in source
(markup/text), not that runtime behavior is correct.

A "consumer" is any directory that directly contains an `index.html`. The linter treats the scan
root itself as a consumer if it has one (so it ships linting its own demo page), plus every
immediate subdirectory that has one. For each consumer it reads `index.html` + every sibling
`*.css` and `*.js` (the checker file excludes itself).

```sh
# Lint this directory's demo page and write the report:
node check.js --md CONFORMANCE.md

# Point it at wherever your consumer pages live:
node check.js path/to/pages
```

C9–C11 are **conditional**: they fire only where a page presents a sealed record, a queue position,
or a charter, then require that pattern's integrity language.

### Exit-code contract (CI-able)

> **Exit code = the number of consumer pages with at least one *unaccepted* failing check.**
> `0` = every page is conformant (or its gaps are baselined) — green, merge.
> `N>0` = N pages have unaccepted gaps — red, fail the build.

So a CI gate is one line:

```yaml
- run: node examples/governed-ui-patterns/check.js examples/governed-ui-patterns
```

A non-zero exit fails the job. Documented exceptions live in the `KNOWN_GAPS` map inside
`check.js`, keyed by consumer name, each with a reason — they render as `◐` (known) and are
excluded from the exit code. Shrink that list over time; never grow it silently.

---

_Demonstration content only. All businesses, names, and data are fictional; every interactive
surface is a simulation. Part of the AzureAgentForge examples._
