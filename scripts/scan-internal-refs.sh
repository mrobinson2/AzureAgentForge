#!/usr/bin/env bash
# Fail if any internal/private reference appears in publishable paths.
#
# Generic secret scanners (gitleaks, trufflehog) don't know which *internal
# names* are sensitive — private hostnames, Key Vault secret conventions, ACR
# and resource-group names, tenant/subscription/org UUIDs, operator paths. This
# scanner encodes that project-specific knowledge. Run it alongside the secret
# scanners, not instead of them.
#
# Scope: AAF-authored, publishable trees only. The upstream submodules under
# apps/*/src are excluded — their contents are governed by their own upstreams.
#
# Usage: scripts/scan-internal-refs.sh        # scan tracked files
#        ALLOWLIST=path scripts/scan-internal-refs.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Publishable code/config trees to scan. `docs/` (prose that legitimately
# discusses architecture) is covered by the secret scanners, not this name-scan.
TARGETS=(apps build/skills scripts services installer infrastructure integrations)

# Out of scope: upstream submodules (own upstreams), binary assets, and this
# scanner itself (its pattern literals would self-match).
EXCLUDES=(
  ':!apps/hermes/src' ':!apps/honcho/src'
  ':!scripts/scan-internal-refs.sh'
  ':!**/*.png' ':!**/*.gif' ':!**/*.jpg'
)

# Internal/PRIVATE (MRTek platform) reference patterns — NOT the public
# AzureAgentForge project's own naming (aafregistry, aaf-vault-*-rg are public).
# Extended regex; tune with the allowlist for legitimate matches.
PATTERNS=(
  # NOTE: ca-<svc>-dev is NOT here — the public deploy uses the same Container App
  # naming, so it is not a private-only token. Genuinely-private tokens only:
  'foundry-mrtek[a-z0-9-]*'                                          # internal Foundry endpoint
  '[Mm][Rr][Tt][Ee][Kk]'                                            # org token
  '[a-z0-9-]+\.mrtek[a-z0-9.-]*'                                    # internal hostnames
  'mrtvault[a-z0-9]*'                                               # private storage account
  'mrt[a-z0-9]*registry'                                           # private ACR names
  'mrt[a-z0-9-]*-rg'                                               # private resource groups
  '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'   # UUID (sub/tenant/org/run/agent id)
  '/Users/[A-Za-z0-9._-]+/'                                        # operator filesystem paths
  '(michael\.?robinson|michaelrobinson[0-9]*)'                    # operator identity
  '\[MRTEK PATCH'                                                  # internal patch marker
)

# Optional allowlist file: one extended-regex per line; matching lines are dropped.
ALLOWLIST="${ALLOWLIST:-$ROOT/.internal-refs-allow}"

hits=0
for pat in "${PATTERNS[@]}"; do
  # -I skip binary, -E extended regex, -n line numbers. git grep honors :! excludes.
  out="$(git -C "$ROOT" grep -nIE "$pat" -- "${TARGETS[@]}" "${EXCLUDES[@]}" 2>/dev/null || true)"
  [ -n "$out" ] || continue
  if [ -f "$ALLOWLIST" ]; then
    # Strip blank lines and comments first — an empty pattern in `grep -f` matches
    # EVERY line, which would silently drop all hits (a false pass).
    allow="$(grep -vE '^[[:space:]]*(#|$)' "$ALLOWLIST" 2>/dev/null || true)"
    [ -n "$allow" ] && out="$(printf '%s\n' "$out" | grep -vEf <(printf '%s\n' "$allow") || true)"
  fi
  [ -n "$out" ] || continue
  echo "── internal-reference pattern matched: /$pat/" >&2
  printf '%s\n' "$out" >&2
  hits=1
done

if [ "$hits" -ne 0 ]; then
  echo "FAIL: internal references found — sanitize before publishing." >&2
  exit 1
fi
echo "OK: no internal references found in publishable paths."
