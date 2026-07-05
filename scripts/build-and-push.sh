#!/usr/bin/env bash
# Build and push the AzureAgentForge service images to an Azure Container Registry.
#
# Uses `az acr build` — the build runs server-side inside ACR and the result is
# pushed there, so NO local Docker daemon is required. The build context is
# uploaded to ACR and built remotely.
#
# Two classes of image:
#
#   self-contained   context = the service directory; builds from this repo alone.
#                    -> model-router, memory-governor, watchdog
#
#   upstream-dependent
#                    context = the repo root; the Dockerfile pulls or COPYs
#                    upstream project sources that are NOT vendored in this repo
#                    (see docs/local-development.md). Each needs apps/<project>/
#                    populated first. This script PREFLIGHTS for those inputs and
#                    refuses to start a build that would fail partway, unless
#                    --skip-unbuildable is given.
#                    -> agent-runtime (image: hermes), honcho, paperclip
#
# Image names match the tags Terraform consumes
# (infrastructure/.../variables.tf *_image_tag): hermes, honcho, router,
# paperclip, memory-governor, watchdog. cloudflared is a public Docker Hub image
# and is never built here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# All buildable services, in build order. cloudflared is intentionally absent.
ALL_SERVICES="model-router memory-governor watchdog teams-bridge agent-runtime honcho paperclip"
SELF_CONTAINED="model-router memory-governor watchdog teams-bridge"

# Default build-args for the paperclip upstream pin (match the Dockerfile ARGs).
# EXPECTED_SHA guards against git-tag drift: the build fails if the cloned tag
# resolves to a different commit. Resolve with:
#   git ls-remote https://github.com/paperclipai/paperclip refs/tags/<tag>
PAPERCLIP_VERSION="${PAPERCLIP_VERSION:-v2026.517.0}"
PAPERCLIP_EXPECTED_SHA="${PAPERCLIP_EXPECTED_SHA:-3e6610fb938d04638fa578a1fc0d119b434fa2e4}"

REGISTRY=""
TAG=""
SERVICES=""
PUSH_LATEST=0
DRY_RUN=0
SKIP_UNBUILDABLE=0

die() { echo "error: $*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: scripts/build-and-push.sh --registry NAME [options]

  -r, --registry NAME    Azure Container Registry name (not the login server). Required.
  -t, --tag TAG          Image tag. Default: short git SHA, else "latest".
  -s, --services LIST    Comma-separated subset to build. Default: all buildable.
      --self-contained   Build only the images this repo can build alone
                         (model-router, memory-governor, watchdog).
      --push-latest      Also tag/push :latest alongside the resolved tag.
      --skip-unbuildable Skip (don't fail) upstream-dependent images whose
                         apps/<project>/ inputs are not vendored.
  -n, --dry-run          Print the az commands without running them.
  -l, --list             Print the service/context table and exit.
  -h, --help             This help.

Examples:
  # Build the three self-contained images and push :<sha> + :latest
  scripts/build-and-push.sh -r myacr --self-contained --push-latest

  # Build everything that can build, skipping un-vendored upstream images
  scripts/build-and-push.sh -r myacr --skip-unbuildable
EOF
}

# image|context(relative to repo root)|dockerfile|class|required-input
svc_meta() {
  case "$1" in
    model-router)    echo "router|services/model-router|services/model-router/Dockerfile|self|" ;;
    memory-governor) echo "memory-governor|services/memory-governor|services/memory-governor/Dockerfile|self|" ;;
    watchdog)        echo "watchdog|services/watchdog|services/watchdog/Dockerfile|self|" ;;
    teams-bridge)    echo "teams-bridge|services/teams-bridge|services/teams-bridge/Dockerfile|self|" ;;
    agent-runtime)   echo "hermes|.|services/agent-runtime/Dockerfile|upstream|apps/hermes/src apps/hermes/overrides/skills" ;;
    honcho)          echo "honcho|.|services/honcho/Dockerfile|upstream|apps/honcho/src apps/honcho/docker-entrypoint.sh" ;;
    paperclip)       echo "paperclip|.|services/paperclip/Dockerfile|upstream|apps/paperclip apps/hermes/src build/skills/skills-manifest.json build/skills/agent-skill-mapping.json" ;;
    *) return 1 ;;
  esac
}

print_table() {
  printf '%-16s %-16s %-12s %-34s %s\n' SERVICE IMAGE CLASS CONTEXT/DOCKERFILE REQUIRES
  for s in $ALL_SERVICES; do
    IFS='|' read -r image context dockerfile class required <<EOF
$(svc_meta "$s")
EOF
    printf '%-16s %-16s %-12s %-34s %s\n' "$s" "$image" "$class" "$context ($dockerfile)" "${required:-—}"
  done
}

resolve_tag() {
  if [ -n "$TAG" ]; then echo "$TAG"; return; fi
  if git -C "$REPO_ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
    git -C "$REPO_ROOT" rev-parse --short HEAD
  else
    echo "latest"
  fi
}

# Auto-init the upstream submodules (apps/*/src) when their content is missing —
# removes the most common footgun: a non-recursive `git clone`. Detect → warn →
# attempt `git submodule update --init --recursive` → continue on success → fail
# with manual instructions only if auto-init fails (e.g. no network, tarball).
ensure_submodules() {
  git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
  [ -f "$REPO_ROOT/.gitmodules" ] || return 0
  local need=0 p
  for p in apps/hermes/src apps/honcho/src; do
    if [ ! -e "$REPO_ROOT/$p/.git" ] && [ -z "$(ls -A "$REPO_ROOT/$p" 2>/dev/null)" ]; then
      need=1
    fi
  done
  [ "$need" -eq 1 ] || return 0
  echo "warning: submodule content missing under apps/*/src — attempting auto-init…" >&2
  if git -C "$REPO_ROOT" submodule update --init --recursive; then
    echo "submodules initialized." >&2
    return 0
  fi
  echo "error: 'git submodule update --init --recursive' failed." >&2
  echo "       Re-clone with:   git clone --recursive <url>" >&2
  echo "       Or run manually: git submodule update --init --recursive" >&2
  return 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    -r|--registry) REGISTRY="${2:-}"; shift 2 ;;
    -t|--tag) TAG="${2:-}"; shift 2 ;;
    -s|--services) SERVICES="${2:-}"; shift 2 ;;
    --self-contained) SERVICES="$(echo "$SELF_CONTAINED" | tr ' ' ',')"; shift ;;
    --push-latest) PUSH_LATEST=1; shift ;;
    --skip-unbuildable) SKIP_UNBUILDABLE=1; shift ;;
    -n|--dry-run) DRY_RUN=1; shift ;;
    -l|--list) print_table; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

# Normalize the requested service list.
if [ -z "$SERVICES" ]; then
  REQUESTED="$ALL_SERVICES"
else
  REQUESTED="$(echo "$SERVICES" | tr ',' ' ')"
fi
for s in $REQUESTED; do
  svc_meta "$s" >/dev/null 2>&1 || die "unknown service '$s'. Known: $ALL_SERVICES"
done

[ -n "$REGISTRY" ] || [ "$DRY_RUN" -eq 1 ] || die "--registry is required (or use --dry-run)"
[ -n "$REGISTRY" ] || REGISTRY="<registry>"
TAG="$(resolve_tag)"

echo "registry=$REGISTRY tag=$TAG dry_run=$DRY_RUN"
echo "requested: $REQUESTED"
echo

# Auto-init submodules only when we actually intend to build an upstream image
# (skip for --self-contained, --dry-run, and --skip-unbuildable runs).
needs_upstream=0
for s in $REQUESTED; do
  case "$s" in agent-runtime|honcho|paperclip) needs_upstream=1 ;; esac
done
if [ "$needs_upstream" -eq 1 ] && [ "$DRY_RUN" -eq 0 ] && [ "$SKIP_UNBUILDABLE" -eq 0 ]; then
  ensure_submodules || die "submodule auto-init failed (see message above)"
fi

built="" ; skipped="" ; failed=""

for s in $REQUESTED; do
  IFS='|' read -r image context dockerfile class required <<EOF
$(svc_meta "$s")
EOF

  # Assemble the az acr build command first so the preflight can show it.
  set -- az acr build --registry "$REGISTRY" --image "$image:$TAG" --file "$dockerfile"
  [ "$PUSH_LATEST" -eq 1 ] && set -- "$@" --image "$image:latest"
  [ "$s" = "paperclip" ] && set -- "$@" --build-arg "PAPERCLIP_VERSION=$PAPERCLIP_VERSION" \
                                        --build-arg "PAPERCLIP_EXPECTED_SHA=$PAPERCLIP_EXPECTED_SHA"
  set -- "$@" "$context"

  # Preflight upstream-dependent inputs. Each upstream service may require SEVERAL
  # inputs (submodule src, AAF overrides, generated manifests) — check them all.
  if [ "$class" = "upstream" ] && [ -n "$required" ]; then
    missing=""
    for r in $required; do [ -e "$REPO_ROOT/$r" ] || missing="$missing $r"; done
    if [ -n "$missing" ]; then
      if [ "$DRY_RUN" -eq 1 ]; then
        echo "SKIP  $s: missing inputs:$missing (would run): $*"
        skipped="$skipped $s"; continue
      elif [ "$SKIP_UNBUILDABLE" -eq 1 ]; then
        echo "SKIP  $s: missing inputs:$missing — see docs/local-development.md"
        skipped="$skipped $s"; continue
      else
        echo "FAIL  $s: missing inputs:$missing — vendor them or pass --skip-unbuildable" >&2
        failed="$failed $s"; continue
      fi
    fi
  fi

  echo "BUILD $s -> $image:$TAG"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    (cd $REPO_ROOT && $*)"
    built="$built $s"
    continue
  fi
  if ( cd "$REPO_ROOT" && "$@" ); then
    built="$built $s"
  else
    echo "FAIL  $s: az acr build returned non-zero" >&2
    failed="$failed $s"
  fi
done

echo
echo "built:  ${built:-none}"
echo "skipped:${skipped:-none}"
echo "failed: ${failed:-none}"

# Emit the resolved tag for the GitHub Actions step that wires it into Terraform.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "tag=$TAG" >> "$GITHUB_OUTPUT"
  echo "built=$(echo "$built" | xargs echo)" >> "$GITHUB_OUTPUT"
fi

[ -z "$failed" ] || exit 1
