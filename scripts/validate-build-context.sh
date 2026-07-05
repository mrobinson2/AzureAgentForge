#!/usr/bin/env bash
# Validate that every `COPY` source in the upstream Dockerfiles resolves in the
# current checkout. Catches a missing submodule or un-ported AAF file BEFORE a
# (slow, server-side) `az acr build` fails partway. Run after a --recursive
# clone / submodule init.
#
# Note: this checks build-context PRESENCE, not runtime correctness — a service
# can pass this and still fail at runtime on a missing import/env. Pair with a
# real build + the smoke checks (see the design spec §7).
#
# Usage: scripts/validate-build-context.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DOCKERFILES=(
  services/agent-runtime/Dockerfile
  services/honcho/Dockerfile
  services/paperclip/Dockerfile
)

fail=0
for df in "${DOCKERFILES[@]}"; do
  [ -f "$ROOT/$df" ] || { echo "MISSING dockerfile: $df" >&2; fail=1; continue; }

  # Read COPY lines, skipping multi-stage internal copies (--from=...). The build
  # context root is the repo root for these images, so sources are repo-relative.
  while IFS= read -r line; do
    # Drop the leading COPY keyword and any --flags (e.g. --chown=, --from=).
    # shellcheck disable=SC2206
    toks=($line)
    srcs=()
    for t in "${toks[@]:1}"; do
      case "$t" in
        COPY) ;;
        --*) ;;            # flags
        *) srcs+=("$t") ;;
      esac
    done
    # Last token is the destination; everything before it is a source.
    n=${#srcs[@]}
    [ "$n" -ge 2 ] || continue
    for ((i = 0; i < n - 1; i++)); do
      src="${srcs[$i]}"
      # Strip a trailing slash for the existence test.
      if [ ! -e "$ROOT/${src%/}" ]; then
        echo "MISSING ($df): COPY source '$src' does not resolve" >&2
        fail=1
      fi
    done
  done < <(grep -E '^[[:space:]]*COPY' "$ROOT/$df" | grep -v -- '--from=')
done

if [ "$fail" -ne 0 ]; then
  echo "FAIL: one or more COPY sources are missing (vendor submodules / port files)." >&2
  exit 1
fi
echo "OK: all COPY sources resolve for the upstream Dockerfiles."
