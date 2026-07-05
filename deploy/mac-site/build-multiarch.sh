#!/usr/bin/env bash
# build-multiarch.sh — publish multi-arch (amd64 + arm64) manifests for the custom
# images so an arm64 host (e.g. an Apple-silicon mini) pulls native arm64 from the
# SAME tags the cloud pulls amd64 from. Until this runs, an arm64 host runs amd64
# under emulation — functional, just slower; this is an optimization, not a
# prerequisite for failover/failback.
#
# Implemented as a standalone, opt-in script rather than rewiring the CI build
# jobs: it can be run by the operator (or wired into the pipeline later) WITHOUT
# changing the default amd64-only build path, so there is zero regression risk to
# the cloud deploy. Run from a machine logged into your registry
# (`az acr login -n your-registry`).
set -euo pipefail

ACR="${ACR_LOGIN_SERVER:-your-registry.azurecr.io}"
TAG="${1:?usage: build-multiarch.sh <tag>   (match the registry build, e.g. 100)}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# One-time binfmt/qemu so an amd64 builder can emit arm64 layers (no-op if set).
docker run --privileged --rm tonistiigi/binfmt --install all >/dev/null 2>&1 || true
docker buildx inspect aaf-multiarch >/dev/null 2>&1 \
  || docker buildx create --name aaf-multiarch --driver docker-container --use
docker buildx use aaf-multiarch

bx() { # <dockerfile> <name> [extra buildx args...]
  local df="$1" name="$2"; shift 2
  echo "-- buildx ${name}:${TAG} (linux/amd64,linux/arm64) --"
  docker buildx build --platform linux/amd64,linux/arm64 \
    -f "$df" -t "${ACR}/${name}:${TAG}" -t "${ACR}/${name}:latest" "$@" --push .
}

bx services/honcho/Dockerfile          honcho          --target production
bx services/model-router/Dockerfile    model-router
bx services/memory-governor/Dockerfile memory-governor
bx services/paperclip/Dockerfile       paperclip

echo
echo "Done — each :${TAG} tag now carries amd64 + arm64."
echo "Pin the *_IMAGE_TAG values in deploy/mac-site/.env to ${TAG}, then"
echo "'docker compose pull' on the host."
